"""Arena experiments: replay seeds for every arm, scored on the four metrics.

  python -m sieve.arena_eval calibrate          # fit the replay noise model from real GCMC
  python -m sieve.arena_eval seeds --n 20       # replay seeds for each baseline arm
  python -m sieve.arena_eval report             # metrics table (+ JSON for the dashboard)

The noise model is fitted only from real measurements:
  tier-2 noise   from independent tier-2 replicas (arena truth top 30 have two)
  tier-1 noise   from structures measured at both tiers: var(t1 - t2) - var(t2)
  reported err   resampled from real RASPA block errors at each tier
  cost noise     log(actual CPU / planned CPU) where the plan already knew the uptake
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics as stats
from dataclasses import asdict
from pathlib import Path

from sieve.arena import ARENA_DB, TRUTH_RUN, arena_structures
from sieve.baselines import run_arm
from sieve.evaluate import Truth, score_run, summarize
from sieve.oracle import PROJECT_ROOT, NoiseModel, Oracle
from sieve.store import Store

REPLAY_DB = PROJECT_ROOT / "data" / "arena-replay.db"
NOISE_JSON = PROJECT_ROOT / "data" / "noise.json"
CALIBRATION_DB = PROJECT_ROOT / "data" / "bench" / "modes.db"
REAL_DBS = [PROJECT_ROOT / "data" / "runs.db", CALIBRATION_DB, ARENA_DB]


def _ok_rows(db: Path):
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    return con.execute("SELECT run_id, structure, tier, uptake_molkg, uptake_err, cpu_s, est_cpu_s, "
                       "result_json FROM evaluations WHERE status='ok' AND tier IN (1, 2) AND "
                       "mode NOT LIKE 'replay%'").fetchall()


def calibrate() -> NoiseModel:
    rows = [r for db in REAL_DBS for r in _ok_rows(db)]
    by = {}
    for run, s, tier, u, e, cpu, est, rj in rows:
        by.setdefault(s, {}).setdefault(tier, []).append(u)
    # tier-2 replica noise (relative)
    t2_rel = [(v[0] - v[1]) / ((v[0] + v[1]) / 2) for d in by.values() if len(v := d.get(2, [])) >= 2
              and sum(v[:2]) > 0.2]
    var2 = (stats.pvariance(t2_rel) / 2) if len(t2_rel) >= 5 else 0.04 ** 2
    # tier-1 vs tier-2 on the same structure
    d12 = [(stats.mean(d[1]) - stats.mean(d[2])) / stats.mean(d[2]) for d in by.values()
           if 1 in d and 2 in d and stats.mean(d[2]) > 0.1]
    var1 = max(var2, stats.pvariance(d12) - var2) if len(d12) >= 8 else 0.08 ** 2
    rep = {t: [round(e / u, 4) for _, _, tt, u, e, *_ in rows if tt == t and u > 0.1] for t in (1, 2)}
    logs = []
    for *_, cpu, est, rj in rows:
        meta = json.loads(rj or "{}")
        if meta.get("uptake_hint") is not None and est and cpu:
            logs.append(math.log(cpu / est))
    nm = NoiseModel(
        rel_sd={1: round(math.sqrt(var1), 4), 2: round(math.sqrt(var2), 4)},
        reported_rel_err={t: v or NoiseModel().reported_rel_err[t] for t, v in rep.items()},
        cost_log_sd=round(stats.pstdev(logs), 3) if len(logs) >= 8 else 0.2,
        source=f"fit: {len(t2_rel)} t2 replica pairs, {len(d12)} t1/t2 pairs, {len(logs)} cost pairs")
    NOISE_JSON.write_text(json.dumps(asdict(nm)))
    return nm


def load_noise() -> NoiseModel:
    d = json.loads(NOISE_JSON.read_text())
    d["rel_sd"] = {int(k): v for k, v in d["rel_sd"].items()}
    d["reported_rel_err"] = {int(k): v for k, v in d["reported_rel_err"].items()}
    return NoiseModel(**d)


def run_seeds(arms: list[str], n: int, budget_s: float, start: int = 0) -> list[str]:
    noise = load_noise()
    model = Oracle(str(CALIBRATION_DB), "calibration", labels_path=None).fit_cost_model()
    arena = arena_structures()
    done = []
    for arm in arms:
        for seed in range(start, start + n):
            rid = f"{arm}-replay-s{seed}"
            st = Store(REPLAY_DB).con.execute("SELECT status FROM runs WHERE run_id=?",
                                              (rid,)).fetchone()
            if st and st[0] == "finished":
                continue
            run_arm(arm, str(REPLAY_DB), budget_s, n_workers=1, seed=seed, run_id=rid,
                    cost_model=model, candidates=arena,
                    replay={"truth_db": str(ARENA_DB), "seed": seed, "noise": asdict(noise)})
            done.append(rid)
    return done


OMNI = "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B"
VARIANTS = {"sieve": {}, "sieve-v4": {}, "sieve-v4-omni": {"scout": OMNI, "subcritic": OMNI}, "sieve-dsv4": {"orchestrator": "deepseek-ai/DeepSeek-V4-Pro",
                                        "allocator": "deepseek-ai/DeepSeek-V4-Pro",
                                        "critic": "deepseek-ai/DeepSeek-V4-Pro"}}


# search-policy variants (same models as v4)
POLICIES = {"sieve-v5": {"upside": True, "random_frac": 0.10, "promise_floor": 0.15}}
VARIANTS["sieve-v5"] = {}


def run_sieve_seeds(n: int, budget_s: float, start: int = 0, parallel: int = 5, arm: str = "sieve") -> list[str]:
    """SIEVE replay seeds. Model calls dominate wall time, so several seeds run concurrently."""
    import asyncio

    from sieve.agents.team import run as run_team

    noise = load_noise()
    model = Oracle(str(CALIBRATION_DB), "calibration", labels_path=None).fit_cost_model()
    arena = arena_structures()
    st = Store(REPLAY_DB)
    todo = []
    for seed in range(start, start + n):
        rid = f"{arm}-replay-s{seed}"
        row = st.con.execute("SELECT status FROM runs WHERE run_id=?", (rid,)).fetchone()
        if not (row and row[0] == "finished"):
            todo.append((seed, rid))

    async def main():
        sem = asyncio.Semaphore(parallel)

        async def one(seed, rid):
            async with sem:
                try:
                    return await run_team(str(REPLAY_DB), budget_s, 1, seed, run_id=rid, cost_model=model,
                                          candidates=arena, arm=arm, models=VARIANTS[arm], policy=POLICIES.get(arm),
                                          replay={"truth_db": str(ARENA_DB), "seed": seed, "noise": asdict(noise)})
                except Exception as e:  # one failed seed must not stop the others
                    print(f"{rid} failed: {type(e).__name__}: {e}", flush=True)
        return await asyncio.gather(*(one(s, r) for s, r in todo))

    return [r for r in asyncio.run(main()) if r]


def report(db: Path = REPLAY_DB, budgets_h: list[float] | None = None) -> dict:
    truth = Truth.load(str(ARENA_DB))
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    runs = [r[0] for r in con.execute("SELECT run_id FROM runs WHERE status='finished' AND "
                                      "run_id NOT IN (?, 'referee')", (TRUTH_RUN,))]
    scores = [score_run(con, r, truth) for r in runs]
    budgets_h = budgets_h or sorted({round(s.budget_h * f, 4) for s in scores for f in (0.25, 0.5, 1)})
    return summarize(scores, truth, budgets_h)


def main() -> None:
    p = argparse.ArgumentParser(prog="sieve.arena_eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("calibrate")
    s = sub.add_parser("seeds")
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--start", type=int, default=0)
    s.add_argument("--budget-h", type=float, default=1.0)
    s.add_argument("--arms", default="random,descriptor,uniform")
    s.add_argument("--parallel", type=int, default=5)
    r = sub.add_parser("report")
    r.add_argument("--db", default=str(REPLAY_DB))
    a = p.parse_args()
    if a.cmd == "calibrate":
        print(calibrate())
    elif a.cmd == "seeds":
        arms = a.arms.split(",")
        for v in [x for x in arms if x in VARIANTS]:
            print(run_sieve_seeds(a.n, a.budget_h * 3600, a.start, a.parallel, v))
        rest = [x for x in arms if x not in VARIANTS]
        if rest:
            print(run_seeds(rest, a.n, a.budget_h * 3600, a.start))
    else:
        print(json.dumps(report(Path(a.db)), indent=2))


if __name__ == "__main__":
    main()

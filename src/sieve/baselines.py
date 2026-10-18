"""Three reference arms. Same pool, same oracle, same cost model, same worker pool, same budget.

  random      shuffled pool, tier 1 on each until the budget is exhausted
  descriptor  tier 0 on every candidate, rank by DESCRIPTOR_SCORE, tier 1 top-down
  uniform     shuffled pool (same seed as random), tier 2 on each until the budget is exhausted

DESCRIPTOR_SCORE was fixed before any arm ran and is never tuned against results:
    score = void_fraction * exp(-(LCD - 5 A)^2 / 8 A^2)
Without framework charges CO2 binds only through overlapping wall potentials, strongest in
cavities ~1.5x its 3.3 A kinetic diameter; void fraction stands in for capacity.

An arm's final ranking is its own measurements: per structure, the inverse-variance mean at the
highest tier it reached. Tier-1 numbers are NOT converged (+/- 18%); the random and descriptor
arms report tier-1 winners, which is exactly the failure mode SIEVE is built to avoid.

Objective "capture" (sieve/capture.py), for a fair comparison with SIEVE under the same objective:
every arm ends with the same final funnel SIEVE's orchestrator runs -- its own top CAPTURE_K (3)
structures, by its own ranking, are confirmed at tier 2 where they are not already, then get a
tier-3 capture check, and the arm recommends the highest APS among them (a `recommend` event).
While screening, an arm holds back what that funnel would cost for its current top 3 (re-estimated
every batch with the shared cost model), as SIEVE's orchestrator does. Baselines run the capture
check on the CIF as given (they have no disorder check; sieve/referee.py reports raw vs resolved).
Default objective "uptake": behaviour exactly as before.
"""

from __future__ import annotations

import json
import math
import os
import random
import time

from sieve import pool
from sieve.oracle import CostModel, Descriptors
from sieve.store import Store, estimates_from_evaluations, resume
from sieve.workers import InlineExecutor, WorkerConfig, WorkerPool, default_workers

ARMS = ("random", "descriptor", "uniform")
MIN_USEFUL_CPU_S = 5.0  # below this nothing fits; stop


def descriptor_score(d: Descriptors) -> float:
    lcd = (d.core or {}).get("LCD")
    vf = (d.core or {}).get("AV_VF")
    if not d.parse_ok or lcd is None:
        return -math.inf
    if vf is None:
        vf = d.void_fraction_geo
    return vf * math.exp(-((lcd - 5.0) ** 2) / 8.0)


def _wait_for(st: Store, wp: WorkerPool, run_id: str, max_inflight: int) -> None:
    while True:
        wp.supervise()
        n = len(st.tasks(run_id, "queued")) + len(st.tasks(run_id, "running"))
        if n < max_inflight:
            return
        time.sleep(0.5)


def _close_rounds(st: Store, run_id: str) -> None:
    """A round commits once every task in it reached a terminal state."""
    for r in st.con.execute("SELECT round_no FROM rounds WHERE run_id=? AND finished_at IS NULL",
                            (run_id,)).fetchall():
        open_ = st.con.execute("SELECT COUNT(*) FROM tasks WHERE run_id=? AND round_no=? AND "
                               "status IN ('queued','running')", (run_id, r[0])).fetchone()[0]
        if open_ == 0:
            est = estimates_from_evaluations(st.con, run_id)
            best = max(est.values(), key=lambda e: e.uptake, default=None)
            spent = st.con.execute("SELECT COALESCE(SUM(cpu_s),0) FROM evaluations WHERE "
                                   "run_id=? AND status!='running'", (run_id,)).fetchone()[0]
            st.finish_round(run_id, r[0], {
                "spent_cpu_s": round(spent, 1), "n_structures": len(est),
                "best": None if best is None else {"structure": best.structure,
                                                   "uptake": best.uptake, "tier": best.tier}})


def run_arm(arm: str, db_path: str, budget_cpu_s: float = 4 * 3600.0, n_workers: int | None = None,
            seed: int = 0, run_id: str | None = None, cost_model: CostModel | None = None,
            limit: int | None = None, candidates: list[str] | None = None,
            replay: dict | None = None, objective: str = "uptake", pool_kind: str = "mof") -> str:
    """candidates: restrict the pool (the arena). replay: run against ReplayOracle, inline.
    objective/pool_kind: see the module docstring and sieve/pool.py; both live in the run config."""
    from sieve.capture import objective as _objective
    assert arm in ARMS, arm
    st = Store(db_path)
    n_workers = n_workers or default_workers()
    if run_id and st.con.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
        state = resume(db_path, run_id)
        budget_cpu_s, seed = state.budget_cpu_s, state.config["seed"]
        n_workers = state.config.get("n_workers") or n_workers
        cost_model = CostModel(**state.config["cost_model"])
        limit = state.config.get("limit")
        candidates = state.config.get("candidates")
        replay = state.config.get("replay")
        objective, pool_kind = _objective(state.config), pool.pool_kind(state.config)
        for k, v in (state.config.get("env") or {}).items():  # e.g. demo CPU-limit knobs
            os.environ[k] = v
    else:
        if objective == "capture" and replay:
            raise ValueError("the capture objective has no replay model")
        cost_model = cost_model or CostModel()
        run_id = st.create_run(arm, budget_cpu_s, {
            "objective": objective,
            "seed": seed, "n_workers": n_workers, "limit": limit, "candidates": candidates,
            "replay": replay, "cost_model": cost_model.__dict__,
            "env": {k: v for k, v in os.environ.items() if k.startswith("SIEVE_TIMEOUT")},
            "pool": "arena" if candidates else "mixed" if pool_kind == "mixed" else "CoRE-2019-ASR, PLD >= 3.3 A"},
            run_id)

    if candidates:
        cands = [pool.Candidate(n, pool.cif_path(n)) for n in candidates]
    elif pool_kind == "mixed":
        sample = st.run(run_id) and json.loads(st.run(run_id)["config_json"]).get("sample")
        if sample:  # stored at creation: a resume screens the same structures
            cands = [pool.Candidate(n, pool.cif_path(n), pool.family_of(n)) for n in sample]
        else:
            cands = pool.sample(pool.load_candidates(pool="mixed"), limit, seed)
            if limit:
                st.con.execute("UPDATE runs SET config_json=json_set(config_json, '$.sample', json(?)) WHERE run_id=?",
                               (json.dumps([c.name for c in cands]), run_id))
        limit = None
    else:
        cands = pool.load_candidates()
    if limit:
        cands = random.Random(seed).sample(cands, min(limit, len(cands)))
    tier = 2 if arm == "uniform" else 1
    cfg = WorkerConfig(db_path, run_id, budget_cpu_s, cost_model, replay=replay)
    Executor = InlineExecutor if replay else WorkerPool
    wp = Executor(cfg, n_workers, parent_agent=f"{arm}-driver")
    st.upsert_agent(run_id, f"{arm}-driver", "orchestrator", model=None, state="working")
    wp.start()
    try:
        if arm == "descriptor":
            order = _descriptor_order(st, wp, run_id, cands, n_workers)
        else:
            order = [c.name for c in cands]
            random.Random(seed).shuffle(order)
        reserve = (lambda: _finale_reserve(st, cfg, run_id)) if objective == "capture" else None
        _screen(st, wp, run_id, order, tier, n_workers, budget_cpu_s, reserve)
        if objective == "capture":
            _capture_finale(st, wp, cfg, run_id, arm)
    finally:
        wp.stop()
        _close_rounds(st, run_id)
    est = estimates_from_evaluations(st.con, run_id)
    ranking = sorted(est.values(), key=lambda e: e.uptake, reverse=True)
    st.set_agent_state(f"{arm}-driver", "exited")
    st.finish_run(run_id, "finished", {
        "n_structures": len(est),
        "top10": [{"structure": e.structure, "uptake": round(e.uptake, 4),
                   "err": round(e.err, 4), "tier": e.tier} for e in ranking[:10]]})
    return run_id


def _descriptor_order(st: Store, wp: WorkerPool, run_id: str, cands, n_workers: int) -> list[str]:
    done = {r[0] for r in st.con.execute("SELECT DISTINCT structure FROM tasks WHERE run_id=? "
                                         "AND tier=0", (run_id,))}
    todo = [c.name for c in cands if c.name not in done]
    if todo:
        st.start_round(run_id, 0, {"phase": "tier0", "n": len(todo)})
        st.queue_tasks(run_id, 0, [{"structure": s, "tier": 0} for s in todo])
    _wait_for(st, wp, run_id, 1)
    _close_rounds(st, run_id)
    import json

    scored = []
    for s, rj in st.con.execute("SELECT structure, result_json FROM evaluations WHERE run_id=? "
                                "AND tier=0", (run_id,)):
        scored.append((descriptor_score(Descriptors.from_dict(json.loads(rj))), s))
    scored.sort(reverse=True)
    st.event(run_id, "decision", {"what": "descriptor ranking", "n": len(scored),
                                  "top5": [s for _, s in scored[:5]]},
             agent_id="descriptor-driver")
    return [s for sc, s in scored if sc > -math.inf]


def _finale_jobs(st: Store, o, cfg: WorkerConfig, run_id: str) -> list[dict]:
    """The funnel's next jobs for an arm's own top CAPTURE_K: tier 2 where missing, else tier 3."""
    from sieve.capture import CAPTURE_K, estimates as cap_est

    est = estimates_from_evaluations(st.con, run_id)
    cap = cap_est(st.con, run_id)
    top = sorted(est.values(), key=lambda e: e.uptake, reverse=True)[:CAPTURE_K]
    asked = {(r[0], r[1]) for r in st.con.execute("SELECT structure, tier FROM tasks WHERE run_id=? AND tier>=2 AND "
                                                  "status IN ('queued','running','done')", (run_id,))}
    jobs = []
    for e in top:
        if e.tier < 2 and (e.structure, 2) not in asked:
            jobs.append({"structure": e.structure, "tier": 2, "reason": "capture funnel: confirm a finalist"})
        elif e.tier >= 2 and e.structure not in cap and (e.structure, 3) not in asked:
            jobs.append({"structure": e.structure, "tier": 3, "reason": "capture funnel: capture check"})
    return jobs


def _finale_reserve(st: Store, cfg: WorkerConfig, run_id: str) -> float:
    """CPU-s the capture funnel would need for the arm's current top 3 (tier 2 if missing + tier 3)."""
    from sieve import capture
    from sieve.workers import load_descriptors, make_oracle

    o = make_oracle(cfg)
    est = estimates_from_evaluations(st.con, run_id)
    have = capture.estimates(st.con, run_id)
    total = 0.0
    for e in sorted(est.values(), key=lambda e: e.uptake, reverse=True)[:capture.CAPTURE_K]:
        if e.structure in have:
            continue
        d = load_descriptors(o, e.structure, cfg.cif_dir, "reserve")
        if not d.parse_ok:
            continue
        if e.tier < 2:
            total += o.plan(d, 2, "auto", cfg.cost_model, e.uptake).est_cpu_s
        total += capture.plan(o, d, cfg.cost_model, e.uptake).est_cpu_s
    return total


def _capture_finale(st: Store, wp, cfg: WorkerConfig, run_id: str, arm: str) -> None:
    """Confirm the arm's top 3 at tier 2, capture-check them, recommend the best APS."""
    from sieve.capture import estimates as cap_est
    from sieve.workers import make_oracle

    o = make_oracle(cfg)
    round_no = (st.con.execute("SELECT COALESCE(MAX(round_no), 0) FROM rounds WHERE run_id=?",
                               (run_id,)).fetchone()[0] or 0) + 1
    for _ in range(4):  # tier 2 pass, tier 3 pass (+ slack if the top changed after confirmation)
        jobs = _finale_jobs(st, o, cfg, run_id)
        if not jobs:
            break
        before = o.spent()
        st.start_round(run_id, round_no, {"phase": "capture-funnel", "n": len(jobs),
                                          "split": ", ".join(f"{j['structure']} t{j['tier']}" for j in jobs)})
        st.queue_tasks(run_id, round_no, [{**j, "priority": 2.0 + j["tier"]} for j in jobs])
        round_no += 1
        _wait_for(st, wp, run_id, 1)
        _close_rounds(st, run_id)
        if o.spent() - before < 1.0:  # every job refused by the budget: stop
            break
    cap = cap_est(st.con, run_id)
    est = estimates_from_evaluations(st.con, run_id)
    ok = [s for s in cap if s in est and est[s].tier >= 2]
    if ok:
        best = max(ok, key=lambda s: cap[s]["aps"])
        c = cap[best]
        st.event(run_id, "recommend", {"structure": best, "objective": "capture", "tier": 3, "aps": c["aps"],
                                       "aps_err": c["aps_err"], "S": c["S"], "dn": c["dn"],
                                       "uptake": est[best].uptake, "err": est[best].err,
                                       "text": f"{arm}: highest APS among its capture-checked top 3 — "
                                               f"{best.replace('_clean', '')}, APS {c['aps']:.3f} mol/kg"},
                 agent_id=f"{arm}-driver")
    else:
        st.event(run_id, "decision", {"text": "capture funnel: no capture-checked finalist (budget)"},
                 agent_id=f"{arm}-driver")


def _screen(st: Store, wp: WorkerPool, run_id: str, order: list[str], tier: int, n_workers: int,
            budget_cpu_s: float, reserve=None) -> None:
    tasked = {r[0] for r in st.con.execute("SELECT DISTINCT structure FROM tasks WHERE run_id=? "
                                           "AND tier=?", (run_id, tier))}
    queue = [s for s in order if s not in tasked]
    round_no = (st.con.execute("SELECT COALESCE(MAX(round_no), 0) FROM rounds WHERE run_id=?",
                               (run_id,)).fetchone()[0] or 0) + 1
    refusals_seen = 0
    while queue:
        _wait_for(st, wp, run_id, 2 * n_workers)
        _close_rounds(st, run_id)
        spent = st.con.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='running' THEN est_cpu_s ELSE cpu_s END),0) "
            "FROM evaluations WHERE run_id=?", (run_id,)).fetchone()[0]
        refused = st.con.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND "
                                 "type='budget_refused'", (run_id,)).fetchone()[0]
        held = reserve() if reserve else 0.0  # capture objective: the funnel's cost is held back
        if budget_cpu_s - spent - held < MIN_USEFUL_CPU_S or refused - refusals_seen > 3 * n_workers:
            st.event(run_id, "decision", {"what": "stop screening", "spent_cpu_s": round(spent, 1),
                                          "refusals": refused, **({"held_for_capture_cpu_s": round(held, 1)}
                                                                  if reserve else {})})
            break
        batch, queue = queue[:n_workers], queue[n_workers:]
        st.start_round(run_id, round_no, {"tier": tier, "n": len(batch)})
        st.queue_tasks(run_id, round_no, [{"structure": s, "tier": tier} for s in batch])
        round_no += 1
    _wait_for(st, wp, run_id, 1)

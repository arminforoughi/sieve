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
"""

from __future__ import annotations

import math
import random
import time

from sieve import pool
from sieve.oracle import CostModel, Descriptors
from sieve.store import Store, estimates_from_evaluations, resume
from sieve.workers import WorkerConfig, WorkerPool, default_workers

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
            limit: int | None = None) -> str:
    assert arm in ARMS, arm
    st = Store(db_path)
    n_workers = n_workers or default_workers()
    if run_id and st.con.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
        state = resume(db_path, run_id)
        budget_cpu_s, seed = state.budget_cpu_s, state.config["seed"]
        cost_model = CostModel(**state.config["cost_model"])
        limit = state.config.get("limit")
    else:
        cost_model = cost_model or CostModel()
        run_id = st.create_run(arm, budget_cpu_s, {
            "seed": seed, "n_workers": n_workers, "limit": limit,
            "cost_model": cost_model.__dict__, "pool": "CoRE-2019-ASR, PLD >= 3.3 A"}, run_id)

    cands = pool.load_candidates()
    if limit:
        cands = random.Random(seed).sample(cands, min(limit, len(cands)))
    tier = 2 if arm == "uniform" else 1
    cfg = WorkerConfig(db_path, run_id, budget_cpu_s, cost_model)
    wp = WorkerPool(cfg, n_workers, parent_agent=f"{arm}-driver")
    st.upsert_agent(run_id, f"{arm}-driver", "orchestrator", model=None, state="working")
    wp.start()
    try:
        if arm == "descriptor":
            order = _descriptor_order(st, wp, run_id, cands, n_workers)
        else:
            order = [c.name for c in cands]
            random.Random(seed).shuffle(order)
        _screen(st, wp, run_id, order, tier, n_workers, budget_cpu_s)
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


def _screen(st: Store, wp: WorkerPool, run_id: str, order: list[str], tier: int, n_workers: int,
            budget_cpu_s: float) -> None:
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
        if budget_cpu_s - spent < MIN_USEFUL_CPU_S or refused - refusals_seen > 3 * n_workers:
            st.event(run_id, "decision", {"what": "stop screening", "spent_cpu_s": round(spent, 1),
                                          "refusals": refused})
            break
        batch, queue = queue[:n_workers], queue[n_workers:]
        st.start_round(run_id, round_no, {"tier": tier, "n": len(batch)})
        st.queue_tasks(run_id, round_no, [{"structure": s, "tier": tier} for s in batch])
        round_no += 1
    _wait_for(st, wp, run_id, 1)

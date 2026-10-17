"""The arena: a fixed random sub-pool small enough to know the truth for.

The full pool (8,794 structures) cannot be ground-truthed on a laptop (~270 CPU-h at tier 2), so
recall, "was the recommended winner really top-k" and reliability across seeds are measured here:
  - ARENA_SIZE structures drawn once (seed ARENA_SEED) from the same candidate pool
  - ground truth = real tier-2 GCMC on every one of them (run "arena-truth"), plus a second
    independent replica for the top TRUTH_REPLICA_TOP so the true leaders are not one noisy draw
  - stored in the `truth` table of the arena DB; only the oracle reads it (see ReplayOracle)

"True" means converged GCMC under SieveUFF, not experiment, and it still carries ~3-5% noise:
ties within that noise are reported as ties.
"""

from __future__ import annotations

import json
import random
import time

from sieve import pool
from sieve.oracle import PROJECT_ROOT, CostModel
from sieve.store import Store, estimates_from_evaluations
from sieve.workers import WorkerConfig, WorkerPool, default_workers

ARENA_SIZE = 300
ARENA_SEED = 1
TRUTH_RUN = "arena-truth"
TRUTH_REPLICA_TOP = 30
ARENA_DB = PROJECT_ROOT / "data" / "arena.db"

TRUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS truth (
    structure TEXT PRIMARY KEY, uptake REAL NOT NULL, err REAL NOT NULL, n INTEGER NOT NULL,
    rank INTEGER NOT NULL
);
"""


def arena_structures() -> list[str]:
    names = [c.name for c in pool.load_candidates()]
    return sorted(random.Random(ARENA_SEED).sample(names, ARENA_SIZE))


def _wait_finished(db: str, run_ids: list[str], poll_s: float = 30.0) -> None:
    st = Store(db)
    while True:
        rows = st.con.execute(
            f"SELECT run_id, status FROM runs WHERE run_id IN ({','.join('?' * len(run_ids))})",
            run_ids).fetchall()
        if len(rows) == len(run_ids) and all(r["status"] == "finished" for r in rows):
            return
        time.sleep(poll_s)


def _measure(st: Store, structures: list[str], replica: int, cost_model: CostModel,
             n_workers: int) -> None:
    done = {r[0] for r in st.con.execute(
        "SELECT structure FROM tasks WHERE run_id=? AND reason=? AND status='done'",
        (TRUTH_RUN, f"replica {replica}"))}
    todo = [s for s in structures if s not in done]
    pending = {r[0] for r in st.con.execute(
        "SELECT structure FROM tasks WHERE run_id=? AND reason=? AND status IN ('queued','running')",
        (TRUTH_RUN, f"replica {replica}"))}
    todo = [s for s in todo if s not in pending]
    rnd = (st.con.execute("SELECT COALESCE(MAX(round_no), -1) FROM rounds WHERE run_id=?",
                          (TRUTH_RUN,)).fetchone()[0]) + 1
    if todo:
        st.start_round(TRUTH_RUN, rnd, {"replica": replica, "n": len(todo)})
        st.queue_tasks(TRUTH_RUN, rnd, [{"structure": s, "tier": 2, "reason": f"replica {replica}"}
                                        for s in todo])
    wp = WorkerPool(WorkerConfig(st.db_path, TRUTH_RUN, 1e9, cost_model), n_workers,
                    parent_agent="arena-truth")
    wp.start()
    try:
        while not wp.idle():
            wp.supervise()
            time.sleep(2.0)
    finally:
        wp.stop()
    if todo:
        st.finish_round(TRUTH_RUN, rnd, {"replica": replica})


def build_truth(db: str = str(ARENA_DB), n_workers: int | None = None,
                cost_model: CostModel | None = None, after: list[str] | None = None,
                after_db: str | None = None) -> dict:
    if after:
        _wait_finished(after_db or db, after)
    st = Store(db)
    st.con.executescript(TRUTH_SCHEMA)
    if not st.con.execute("SELECT 1 FROM runs WHERE run_id=?", (TRUTH_RUN,)).fetchone():
        st.create_run("truth", 1e9, {"arena_size": ARENA_SIZE, "arena_seed": ARENA_SEED,
                                     "note": "ground truth, outside every arm's budget"},
                      TRUTH_RUN)
    else:
        from sieve.store import resume
        resume(db, TRUTH_RUN)
    n_workers = n_workers or default_workers()
    cost_model = cost_model or CostModel()
    arena = arena_structures()
    _measure(st, arena, 1, cost_model, n_workers)
    est = estimates_from_evaluations(st.con, TRUTH_RUN)
    lead = sorted(est.values(), key=lambda e: e.uptake, reverse=True)[:TRUTH_REPLICA_TOP]
    _measure(st, [e.structure for e in lead], 2, cost_model, n_workers)
    est = estimates_from_evaluations(st.con, TRUTH_RUN)
    ranked = sorted(est.values(), key=lambda e: e.uptake, reverse=True)
    st.con.execute("DELETE FROM truth")
    for i, e in enumerate(ranked):
        st.con.execute("INSERT INTO truth VALUES (?,?,?,?,?)",
                       (e.structure, e.uptake, e.err, e.n_evals, i + 1))
    missing = sorted(set(arena) - set(est))
    st.finish_run(TRUTH_RUN, "finished", {"n_truth": len(ranked), "missing": missing})
    return {"n_truth": len(ranked), "missing": missing,
            "top5": [(e.structure, round(e.uptake, 3), round(e.err, 3)) for e in ranked[:5]]}


if __name__ == "__main__":
    import argparse

    from sieve.oracle import Oracle

    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(ARENA_DB))
    p.add_argument("--after", default="", help="comma-separated run_ids to wait for")
    p.add_argument("--after-db", default=str(PROJECT_ROOT / "data" / "runs.db"))
    p.add_argument("--calibration", default=str(PROJECT_ROOT / "data" / "bench" / "modes.db"))
    p.add_argument("--workers", type=int, default=None)
    a = p.parse_args()
    model = Oracle(a.calibration, "calibration", labels_path=None).fit_cost_model()
    print(json.dumps(build_truth(a.db, a.workers, model, [r for r in a.after.split(",") if r],
                                 a.after_db), indent=2), flush=True)

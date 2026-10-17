"""Post-hoc referee: how good were each arm's picks, really?

There are no held-out labels for CoRE MOF 2019 under this force field, so the referee builds
them for the structures that matter: the union of every finished arm's top-K. Each gets fresh,
independent tier-2 GCMC (different random seeds, its own run and grid namespace) -- never the
arms' own numbers, which carry the winner's curse: a structure tops a noisy screen partly
because it was measured high. Contenders within error of the best get a second replica.

Arms are scored on the referee values of their picks:
  pick1        referee uptake of the arm's #1 structure
  top10_mean   mean referee uptake of its top 10
  hits         how many of the referee top 10 (within the union) the arm's top 10 contains
  claimed      what the arm said its #1 was worth (inflation = claimed - pick1)

Nothing here is reachable by agents: it runs after the arms finish and writes only the
`referee` and `reference` tables, which no agent prompt reads.
"""

from __future__ import annotations

import json
import time

from sieve.oracle import CostModel
from sieve.store import Store, estimates_from_evaluations
from sieve.workers import WorkerConfig, WorkerPool, default_workers

REFEREE_RUN = "referee"
SCHEMA = """
CREATE TABLE IF NOT EXISTS referee (
    structure TEXT PRIMARY KEY, uptake REAL, err REAL, n INTEGER, updated_at REAL
);
CREATE TABLE IF NOT EXISTS reference (
    value REAL NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def arm_rankings(st: Store, k: int) -> dict[str, list]:
    out = {}
    for r in st.con.execute("SELECT run_id FROM runs WHERE status='finished' AND arm!='referee'"):
        est = estimates_from_evaluations(st.con, r[0])
        out[r[0]] = sorted(est.values(), key=lambda e: e.uptake, reverse=True)[:k]
    return out


def _measure(st: Store, structures: list[str], cost_model: CostModel, n_workers: int,
             replica: int) -> None:
    if not structures:
        return
    if not st.con.execute("SELECT 1 FROM runs WHERE run_id=?", (REFEREE_RUN,)).fetchone():
        st.create_run("referee", 1e9, {"note": "post-hoc, outside every arm's budget"},
                      REFEREE_RUN)
    rnd = (st.con.execute("SELECT COALESCE(MAX(round_no), -1) FROM rounds WHERE run_id=?",
                          (REFEREE_RUN,)).fetchone()[0]) + 1
    st.start_round(REFEREE_RUN, rnd, {"replica": replica, "n": len(structures)})
    st.queue_tasks(REFEREE_RUN, rnd, [{"structure": s, "tier": 2, "reason": f"replica {replica}"}
                                      for s in structures])
    wp = WorkerPool(WorkerConfig(st.db_path, REFEREE_RUN, 1e9, cost_model), n_workers,
                    parent_agent="referee")
    wp.start()
    try:
        while not wp.idle():
            wp.supervise()
            time.sleep(1.0)
    finally:
        wp.stop()
    st.finish_round(REFEREE_RUN, rnd, {"replica": replica})


def run(db_path: str, k: int = 10, n_workers: int | None = None,
        cost_model: CostModel | None = None) -> dict:
    st = Store(db_path)
    st.con.executescript(SCHEMA)
    cost_model = cost_model or CostModel()
    n_workers = n_workers or default_workers()
    ranks = arm_rankings(st, k)
    union = sorted({e.structure for picks in ranks.values() for e in picks})
    have = {r[0] for r in st.con.execute(
        "SELECT DISTINCT structure FROM evaluations WHERE run_id=? AND tier=2 AND status='ok'",
        (REFEREE_RUN,))}
    _measure(st, [s for s in union if s not in have], cost_model, n_workers, 1)

    ref = estimates_from_evaluations(st.con, REFEREE_RUN)
    if ref:  # second replica for contenders within 2 sigma of the referee best
        top = max(ref.values(), key=lambda e: e.uptake)
        close = [e.structure for e in ref.values() if e.n_evals < 2 and
                 e.uptake + 2 * e.err >= top.uptake - 2 * top.err]
        _measure(st, close, cost_model, n_workers, 2)
        ref = estimates_from_evaluations(st.con, REFEREE_RUN)

    now = time.time()
    for e in ref.values():
        st.con.execute("INSERT OR REPLACE INTO referee VALUES (?,?,?,?,?)",
                       (e.structure, e.uptake, e.err, e.n_evals, now))
    best = max(ref.values(), key=lambda e: e.uptake, default=None)
    if best:
        st.con.execute("INSERT INTO reference VALUES (?,?,?)",
                       (best.uptake, f"referee tier-2, best of {len(union)} arm picks", now))
    ref_top = {e.structure for e in sorted(ref.values(), key=lambda e: e.uptake,
                                           reverse=True)[:k]}
    report = {"union": len(union), "best": None if not best else
              {"structure": best.structure, "uptake": best.uptake, "err": best.err}, "arms": {}}
    for run_id, picks in ranks.items():
        vals = [ref[p.structure].uptake for p in picks if p.structure in ref]
        report["arms"][run_id] = {
            "pick1": picks[0].structure if picks else None,
            "pick1_referee": ref[picks[0].structure].uptake if picks and picks[0].structure in ref
            else None,
            "claimed": picks[0].uptake if picks else None,
            "claimed_tier": picks[0].tier if picks else None,
            "top10_mean": sum(vals) / len(vals) if vals else None,
            "hits": len(ref_top & {p.structure for p in picks}),
        }
    st.event(REFEREE_RUN, "decision", {"what": "referee report", **report})
    return report


if __name__ == "__main__":
    import sys

    from sieve.oracle import Oracle

    db = sys.argv[1]
    calib = sys.argv[2] if len(sys.argv) > 2 else None
    model = Oracle(calib, "calibration", labels_path=None).fit_cost_model() if calib else None
    print(json.dumps(run(db, cost_model=model), indent=2))

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

Disorder (sieve/disorder.py): `disorder_report()` gives, for every finished arm's winner, the raw
value next to the value on its disorder-resolved CIF (<stem>_dres_clean, one atom of each
overlapping pair dropped), both fresh referee tier-2 measurements; `--disorder` measures what is
missing. It writes the `referee_disorder` table and never changes how the arms ran.

Nothing here is reachable by agents: it runs after the arms finish and writes only the
`referee` and `reference` tables, which no agent prompt reads.
"""

from __future__ import annotations

import json
import time

from sieve.oracle import CostModel
from sieve.pool import is_resolved
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
CREATE TABLE IF NOT EXISTS referee_disorder (
    run_id TEXT PRIMARY KEY, structure TEXT, disordered INTEGER, clash_atom_fraction REAL,
    raw REAL, raw_err REAL, resolved REAL, resolved_err REAL, drop_frac REAL, updated_at REAL
);
"""


def arm_rankings(st: Store, k: int) -> dict[str, list]:
    out = {}
    for r in st.con.execute("SELECT run_id FROM runs WHERE status='finished' AND arm!='referee'"):
        est = estimates_from_evaluations(st.con, r[0])
        out[r[0]] = sorted((e for e in est.values() if not is_resolved(e.structure)),
                           key=lambda e: e.uptake, reverse=True)[:k]
    return out


def winner(st: Store, run_id: str) -> str | None:
    """The arm's named winner: its last `recommend` event, else its top estimate."""
    import json as _j
    rec = st.con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='recommend' "
                         "ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone()
    if rec and _j.loads(rec[0]).get("structure"):
        return _j.loads(rec[0])["structure"]
    est = estimates_from_evaluations(st.con, run_id)
    best = max((e for e in est.values() if not is_resolved(e.structure)), key=lambda e: e.uptake, default=None)
    return best.structure if best else None


def disorder_report(db_path: str, measure: bool = False, n_workers: int | None = None,
                    cost_model: CostModel | None = None) -> list[dict]:
    """Per finished arm: winner, disorder flag, raw and disorder-resolved referee tier-2 values.
    measure=True runs the missing referee measurements (raw and resolved) first."""
    from sieve import disorder
    from sieve.pool import cif_path, resolved_name

    st = Store(db_path)
    st.con.executescript(SCHEMA)
    scan = disorder.load()
    wins = {r[0]: winner(st, r[0]) for r in st.con.execute(
        "SELECT run_id FROM runs WHERE status='finished' AND arm!='referee'")}
    wins = {k: v for k, v in wins.items() if v}

    def flag(s):
        d = scan.get(s) or disorder.scan(cif_path(s))
        return d
    flags = {s: flag(s) for s in set(wins.values())}
    if measure:
        ref = estimates_from_evaluations(st.con, REFEREE_RUN)
        todo = sorted({s for s in wins.values() if s not in ref}
                      | {resolved_name(s) for s, f in flags.items() if f.get("disordered") and resolved_name(s) not in ref})
        _measure(st, todo, cost_model or CostModel(), n_workers or default_workers(), 1)
    ref = estimates_from_evaluations(st.con, REFEREE_RUN)
    out, now = [], time.time()
    for run_id, s in sorted(wins.items()):
        f = flags[s]
        raw, res = ref.get(s), ref.get(resolved_name(s)) if f.get("disordered") else None
        row = {"run_id": run_id, "structure": s, "disordered": bool(f.get("disordered")),
               "clash_atom_fraction": f.get("clash_atom_fraction"),
               "raw": raw.uptake if raw else None, "raw_err": raw.err if raw else None,
               "resolved": res.uptake if res else (raw.uptake if raw and not f.get("disordered") else None),
               "resolved_err": res.err if res else (raw.err if raw and not f.get("disordered") else None)}
        row["drop_frac"] = ((row["raw"] - row["resolved"]) / row["raw"]
                            if row["raw"] and row["resolved"] is not None else None)
        st.con.execute("INSERT OR REPLACE INTO referee_disorder VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (run_id, s, int(row["disordered"]), row["clash_atom_fraction"], row["raw"], row["raw_err"],
                        row["resolved"], row["resolved_err"], row["drop_frac"], now))
        out.append(row)
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

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    db = args[0]
    calib = args[1] if len(args) > 1 else None
    model = Oracle(calib, "calibration", labels_path=None).fit_cost_model() if calib else None
    if "--disorder" in sys.argv:  # raw vs disorder-resolved referee value of every arm's winner
        print(json.dumps(disorder_report(db, measure=True, cost_model=model), indent=2))
    else:
        print(json.dumps(run(db, cost_model=model), indent=2))

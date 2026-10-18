"""python -m sieve <command>

  baseline <random|descriptor|uniform> [--budget-h 4] [--workers N] [--seed 0] [--limit N]
           [--objective uptake|capture] [--pool mof|mixed]
  sieve [--arm sieve-v5] [--objective uptake|capture] [--pool mof|mixed] [--limit N]
  resume <run_id>          continue a run from its last committed state
  status [run_id]          budget, rounds, evaluations by tier, timeouts
  dashboard [--port 8770] [--demo-replay RUN_ID]
"""

from __future__ import annotations

import argparse
import os

from sieve.oracle import PROJECT_ROOT

DEFAULT_DB = os.environ.get("SIEVE_DB", str(PROJECT_ROOT / "data" / "runs.db"))


def _cost_model(path: str | None):
    from sieve.oracle import CostModel, Oracle

    if not path:
        return CostModel()
    return Oracle(path, "calibration", labels_path=None).fit_cost_model()


def cmd_baseline(a) -> None:
    from sieve.baselines import run_arm

    model = _cost_model(a.calibration)
    print(f"cost model: {model}")
    run_id = run_arm(a.arm, a.db, a.budget_h * 3600.0, a.workers, a.seed, cost_model=model,
                     limit=a.limit, run_id=a.run_id, objective=a.objective, pool_kind=a.pool)
    print(f"finished {run_id}")
    cmd_status(argparse.Namespace(db=a.db, run_id=run_id))


def cmd_resume(a) -> None:
    from sieve.baselines import ARMS, run_arm
    from sieve.store import Store

    st = Store(a.db)
    arm = st.run(a.run_id)["arm"]
    if not a.auto:  # a person typed this: that is an intervention, and the dashboard counts it
        st.event(a.run_id, "intervention", {"text": "manual resume from the command line"})
    if arm.startswith("sieve"):  # every agent variant; its models and policy are in the run config
        from sieve.agents.team import run_sync
        run_sync(a.db, run_id=a.run_id)
    elif arm in ARMS:
        run_arm(arm, a.db, run_id=a.run_id)
    else:
        raise SystemExit(f"resume for arm {arm!r} is not implemented yet")
    cmd_status(argparse.Namespace(db=a.db, run_id=a.run_id))


def cmd_status(a) -> None:
    from sieve.store import Store, resume

    st = Store(a.db)
    runs = [a.run_id] if a.run_id else [r[0] for r in st.con.execute(
        "SELECT run_id FROM runs ORDER BY created_at")]
    for rid in runs:
        s = resume(a.db, rid, reconcile=False)
        by_tier = dict(st.con.execute(
            "SELECT tier, COUNT(*) FROM evaluations WHERE run_id=? AND status='ok' GROUP BY tier",
            (rid,)).fetchall())
        n_to = st.con.execute("SELECT COUNT(*) FROM timeouts WHERE run_id=?", (rid,)).fetchone()[0]
        best = sorted(s.estimates.values(), key=lambda e: e.uptake, reverse=True)[:5]
        print(f"{rid}  [{s.arm}, {s.status}]  spent {s.spent_cpu_s / 3600:.3f} / "
              f"{s.budget_cpu_s / 3600:.2f} CPU-h  rounds {s.last_completed_round + 1}  "
              f"evals t0/t1/t2 {by_tier.get(0, 0)}/{by_tier.get(1, 0)}/{by_tier.get(2, 0)}"
              + (f"/t3 {by_tier[3]}" if by_tier.get(3) else "") + "  "
              f"timeouts {n_to}")
        for e in best:
            print(f"    {e.structure:40s} {e.uptake:7.3f} +/- {e.err:.3f} mol/kg  (tier {e.tier})")
        if by_tier.get(3):
            from sieve.capture import estimates as cap_est
            for s_, c in sorted(cap_est(st.con, rid).items(), key=lambda kv: -kv[1]["aps"]):
                print(f"    {s_:40s} APS {c['aps']:7.3f} +/- {c['aps_err']:.3f}  S {c['S']:.1f}  "
                      f"dN {c['dn']:.3f} mol/kg  (tier 3)")
        rec = st.con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='recommend' "
                             "ORDER BY event_id DESC LIMIT 1", (rid,)).fetchone()
        if rec:
            import json as _json
            print(f"    recommended: {_json.loads(rec[0]).get('structure')}")


def cmd_sieve(a) -> None:
    from sieve.agents.team import run_sync

    from sieve.arena_eval import POLICIES, VARIANTS

    model = _cost_model(a.calibration)
    run_id = run_sync(a.db, a.budget_h * 3600.0, a.workers, a.seed, run_id=a.run_id, cost_model=model,
                      arm=a.arm, models=VARIANTS.get(a.arm), policy=POLICIES.get(a.arm),
                      objective=a.objective, pool_kind=a.pool, limit=a.limit,
                      disorder_check=False if a.no_disorder_check else None)
    print(f"finished {run_id}")
    cmd_status(argparse.Namespace(db=a.db, run_id=run_id))


def cmd_dashboard(a) -> None:
    from sieve.dashboard.server import serve

    print(f"SIEVE dashboard on http://127.0.0.1:{a.port}  (db {a.db})"
          + (f"  replaying {a.demo_replay} at 20x" if a.demo_replay else ""))
    serve(a.db, a.port, a.demo_replay)


def main() -> None:
    p = argparse.ArgumentParser(prog="sieve")
    p.add_argument("--db", default=DEFAULT_DB)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline")
    b.add_argument("arm", choices=["random", "descriptor", "uniform"])
    b.add_argument("--budget-h", type=float, default=4.0)
    b.add_argument("--workers", type=int, default=None)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--limit", type=int, default=None, help="screen a random subset (smoke runs)")
    b.add_argument("--run-id", default=None)
    b.add_argument("--calibration", default=None, help="DB to fit the shared cost model from")
    b.add_argument("--objective", choices=["uptake", "capture"], default="uptake",
                   help="capture: end with the same tier-3 capture check of the top 3 as SIEVE, rank by APS")
    b.add_argument("--pool", choices=["mof", "mixed"], default="mof", help="mixed: CoRE MOFs + IZA zeolites")
    b.set_defaults(fn=cmd_baseline)
    sv = sub.add_parser("sieve", help="run the agent team")
    sv.add_argument("--budget-h", type=float, default=4.0)
    sv.add_argument("--workers", type=int, default=None)
    sv.add_argument("--seed", type=int, default=0)
    sv.add_argument("--run-id", default=None)
    sv.add_argument("--calibration", default=None)
    sv.add_argument("--arm", default="sieve", help="agent variant: sieve, sieve-v5, sieve-dsv4, ... (see arena_eval)")
    sv.add_argument("--objective", choices=["uptake", "capture"], default="uptake",
                    help="capture: tier-3 capture checks, recommend by APS = S(CO2/N2) x dN")
    sv.add_argument("--pool", choices=["mof", "mixed"], default="mof", help="mixed: CoRE MOFs + IZA zeolites")
    sv.add_argument("--limit", type=int, default=None, help="screen a random (family-stratified) subset (smoke runs)")
    sv.add_argument("--no-disorder-check", action="store_true",
                    help="do not re-measure disordered finalists on their resolved CIFs")
    sv.set_defaults(fn=cmd_sieve)
    r = sub.add_parser("resume")
    r.add_argument("run_id")
    r.add_argument("--auto", action="store_true", help="issued by a supervisor, not a person")
    r.set_defaults(fn=cmd_resume)
    s = sub.add_parser("status")
    s.add_argument("run_id", nargs="?")
    s.set_defaults(fn=cmd_status)
    d = sub.add_parser("dashboard")
    d.add_argument("--port", type=int, default=8770)
    d.add_argument("--demo-replay", default=None, metavar="RUN_ID")
    d.set_defaults(fn=cmd_dashboard)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()

import os
import signal
import subprocess
import sys
import textwrap
import time

from sieve.oracle import Oracle, bundled_cif_dir
from sieve.store import Store, resume

CIF = bundled_cif_dir() / "IRMOF-1.cif"


def test_events_and_tasks_roundtrip(tmp_path):
    st = Store(tmp_path / "s.db")
    run = st.create_run("random", 100.0, {"seed": 1})
    st.start_round(run, 0, {"n": 2})
    ids = st.queue_tasks(run, 0, [{"structure": "A", "tier": 1}, {"structure": "B", "tier": 2,
                                                                   "priority": 5}])
    first = st.claim_task(run, "w0")
    assert first["structure"] == "B"  # higher priority first
    st.finish_task(first["task_id"], "done", None)
    st.finish_round(run, 0, {"best": 1.0})
    state = resume(tmp_path / "s.db", run)
    assert state.last_completed_round == 0
    assert [t["task_id"] for t in state.queued] == [ids[0]]
    assert {e["type"] for e in st.events(run)} >= {"run_start", "round_start", "round_end",
                                                   "run_resume"}


CRASHER = textwrap.dedent("""
    import sys
    from sieve.oracle import Oracle, bundled_cif_dir
    from sieve.store import Store
    db, prep = sys.argv[1], sys.argv[2]
    st = Store(db)
    run = st.create_run("random", 3600.0, {}, run_id="crash")
    o = Oracle(db, run, budget_cpu_s=3600.0, labels_path=None, prepared_dir=prep)
    desc = o.tier0(bundled_cif_dir() / "IRMOF-1.cif")
    st.start_round(run, 0)
    t1, t2 = st.queue_tasks(run, 0, [{"structure": desc.structure, "tier": 1},
                                     {"structure": desc.structure, "tier": 1}])
    for _ in range(2):
        task = st.claim_task(run, "w0")
        ev = o.simulate(desc, 1, mode="direct", worker_id="w0")
        st.finish_task(task["task_id"], "done", ev.eval_id)
        print("done", ev.eval_id, flush=True)
""")


def _children(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_kill9_mid_gcmc_then_resume(tmp_path):
    db, prep = tmp_path / "c.db", tmp_path / "prep"
    proc = subprocess.Popen([sys.executable, "-c", CRASHER, str(db), str(prep)],
                            stdout=subprocess.PIPE, text=True)
    first = proc.stdout.readline()  # first GCMC completed and committed
    assert first.startswith("done")
    time.sleep(3.0)                 # second GCMC is now mid-run
    jobs = _children(proc.pid)
    assert jobs, "no RASPA job running at kill time"
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()

    # the orphaned RASPA child notices its parent died and exits
    deadline = time.time() + 5
    while any(map(_alive, jobs)) and time.time() < deadline:
        time.sleep(0.2)
    assert not any(map(_alive, jobs)), "orphaned RASPA job still running"

    o = Oracle(db, "crash", labels_path=None, prepared_dir=prep)
    before = o.con.execute("SELECT status, COUNT(*) FROM evaluations WHERE tier=1 GROUP BY 1"
                           ).fetchall()
    assert dict((r[0], r[1]) for r in before) == {"ok": 1, "running": 1}

    state = resume(db, "crash")
    assert state.reconciled["lost_evaluations"] == 1
    assert 0.0 <= state.reconciled["lost_cpu_s"] <= 5.0 + 0.5  # bounded by heartbeat interval
    assert state.reconciled["requeued_tasks"] == 1 and len(state.queued) == 1
    assert len(state.estimates) == 1  # the completed evaluation survived
    ok_cpu = o.con.execute("SELECT SUM(cpu_s) FROM evaluations WHERE status IN ('ok','lost')"
                           ).fetchone()[0]
    assert abs(state.spent_cpu_s - ok_cpu) < 1e-6  # nothing still counted as an estimate
    assert abs(o.spent() - state.spent_cpu_s) < 1e-6

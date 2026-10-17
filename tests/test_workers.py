import os
import signal
import time

from sieve.oracle import CostModel, Oracle, bundled_cif_dir
from sieve.store import Store
from sieve.workers import WorkerConfig, WorkerPool


def test_dead_worker_is_restarted_and_its_task_completes(tmp_path):
    db = tmp_path / "w.db"
    st = Store(db)
    run = st.create_run("random", 3600.0, {}, run_id="w")
    o = Oracle(db, run, labels_path=None, prepared_dir=tmp_path)
    o.tier0(bundled_cif_dir() / "IRMOF-1.cif")  # descriptors cached in this run
    st.start_round(run, 0)
    (tid,) = st.queue_tasks(run, 0, [{"structure": "IRMOF-1", "tier": 1, "mode": "direct"}])

    wp = WorkerPool(WorkerConfig(str(db), run, 3600.0, CostModel()), n=1)
    wp.start()
    try:
        deadline = time.time() + 30
        while st.con.execute("SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()[0] \
                != "running" and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(2.0)  # mid-GCMC
        _, proc = wp.procs[0]
        os.kill(proc.pid, signal.SIGKILL)
        proc.join()
        recovered = wp.supervise()
        assert recovered and recovered[0]["requeued_tasks"] == 1
        assert recovered[0]["lost_evaluations"] == 1
        deadline = time.time() + 60
        while st.con.execute("SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()[0] \
                != "done" and time.time() < deadline:
            wp.supervise()
            time.sleep(0.2)
    finally:
        wp.stop()
    row = st.con.execute("SELECT status, attempts, worker_id FROM tasks WHERE task_id=?",
                         (tid,)).fetchone()
    assert row["status"] == "done" and row["attempts"] == 2 and row["worker_id"] == "worker-0.r1"
    types = [e["type"] for e in st.events(run)]
    assert "worker_dead" in types and "worker_restart" in types and "task_done" in types
    statuses = sorted(r[0] for r in st.con.execute(
        "SELECT status FROM evaluations WHERE run_id=? AND tier=1", (run,)))
    assert statuses == ["lost", "ok"]

"""Worker pool: one process per usable core, each executing GCMC tasks from the tasks table.

Default size is (performance cores - 1). GCMC is single-threaded and CPU-bound, so running more
jobs than fast cores only stretches every job's wall time; on Apple silicon the efficiency cores
also burn CPU-seconds 2-3x faster for the same work, which would bill the budget for nothing.

A GCMC timeout is normal operation: the oracle kills the job at its CPU limit and the worker
records a 'timeout' event and moves on. A worker *dying* is handled by the supervisor: the dead
worker's in-flight evaluation is marked lost (charged its heartbeat CPU), its task is re-queued,
and a replacement worker starts. Workers exit by themselves if the supervisor dies.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import platform
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass

from sieve import pool
from sieve.oracle import BudgetExhausted, CostModel, Descriptors, Oracle
from sieve.store import Store

POLL_S = 0.2


def default_workers() -> int:
    if platform.system() == "Darwin":
        try:
            p = int(subprocess.run(["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                                   capture_output=True, text=True, check=True).stdout)
            return max(1, p - 1)
        except (subprocess.CalledProcessError, ValueError):
            pass
    return max(1, (os.cpu_count() or 2) - 1)


@dataclass(frozen=True)
class WorkerConfig:
    db_path: str
    run_id: str
    budget_cpu_s: float
    cost_model: CostModel
    cif_dir: str = str(pool.CIF_DIR)
    replay: dict | None = None  # {"truth_db", "seed", "noise": NoiseModel fields}: ReplayOracle


def _watch_parent(parent: int) -> None:
    while True:
        time.sleep(1.0)
        if os.getppid() != parent:
            os._exit(3)


def load_descriptors(o: Oracle, structure: str, cif_dir: str, worker_id: str) -> Descriptors:
    """This run's tier-0 result for the structure, or run (and pay for) tier 0 now."""
    import json

    row = o.con.execute(
        "SELECT result_json FROM evaluations WHERE run_id=? AND structure=? AND tier=0 "
        "ORDER BY started_at DESC LIMIT 1", (o.run_id, structure)).fetchone()
    if row and row[0]:
        d = Descriptors.from_dict(json.loads(row[0]))
        if not d.parse_ok or os.path.exists(d.prepared_path):
            return d
    return o.tier0(pool.cif_path(structure, cif_dir), worker_id=worker_id)


def make_oracle(cfg: WorkerConfig) -> Oracle:
    replay = getattr(cfg, "replay", None)  # getattr: configs pickled by an older parent
    if replay:
        from sieve.oracle import NoiseModel, ReplayOracle
        return ReplayOracle(cfg.db_path, cfg.run_id, replay["truth_db"],
                            NoiseModel(**replay["noise"]), replay["seed"],
                            budget_cpu_s=cfg.budget_cpu_s, cost_model=cfg.cost_model)
    return Oracle(cfg.db_path, cfg.run_id, budget_cpu_s=cfg.budget_cpu_s, labels_path=None)


def run_task(st: Store, o: Oracle, cfg: WorkerConfig, worker_id: str, task) -> None:
    """Execute one claimed task and record its outcome. Shared by pool workers and inline runs."""
    tid, s, tier = task["task_id"], task["structure"], task["tier"]
    st.set_agent_state(worker_id, "working")
    try:
        desc = load_descriptors(o, s, cfg.cif_dir, worker_id)
        if tier == 0:  # descriptors only; no per-structure event (thousands of these)
            st.finish_task(tid, "done" if desc.parse_ok else "error", None)
            return
        if not desc.parse_ok:
            st.finish_task(tid, "error", None)
            st.event(cfg.run_id, "task_error", {"task": tid, "structure": s, "tier": tier,
                                                "error": desc.error}, agent_id=worker_id)
            return
        if tier == 3:  # capture check: binary CO2/N2 + 0.01 bar desorption (sieve/capture.py)
            from sieve import capture
            # a disorder-resolved variant has no 1-bar result of its own yet: plan from the original's
            hint = o.known_uptake(s) or o.known_uptake(s.replace("_dres", ""))
            cp = capture.plan(o, desc, cfg.cost_model, hint)
            if o.remaining() < cp.est_cpu_s:
                raise BudgetExhausted(f"{s} tier 3: needs ~{cp.est_cpu_s:.0f}s, {o.remaining():.0f}s left")
            st.event(cfg.run_id, "task_start", {
                "task": tid, "structure": s, "tier": 3, "mode": "capture", "est_cpu_s": round(cp.est_cpu_s, 1),
                "build_grid": False, "limit_cpu_s": round(capture.cpu_limit(o, cp), 1),
                "n_atoms_sim": desc.n_atoms_sim}, agent_id=worker_id)
            ev = capture.run(o, desc, worker_id=worker_id, cost_model=cfg.cost_model, uptake_hint=hint)
        else:
            plan = o.plan(desc, tier, task["mode"], cfg.cost_model)
            if o.remaining() < plan.est_cpu_s:
                raise BudgetExhausted(f"{s} tier {tier}: needs ~{plan.est_cpu_s:.0f}s, "
                                      f"{o.remaining():.0f}s left")
            st.event(cfg.run_id, "task_start", {
                "task": tid, "structure": s, "tier": tier, "mode": plan.mode,
                "est_cpu_s": round(plan.est_cpu_s, 1), "build_grid": plan.build_grid,
                "limit_cpu_s": round(o.cpu_limit(plan, tier), 1),
                "n_atoms_sim": desc.n_atoms_sim}, agent_id=worker_id)
            ev = o.simulate(desc, tier, task["mode"], worker_id=worker_id, cost_model=cfg.cost_model)
    except BudgetExhausted as e:
        st.finish_task(tid, "skipped", None)
        st.event(cfg.run_id, "budget_refused", {"task": tid, "structure": s, "tier": tier,
                                                "reason": str(e)}, agent_id=worker_id)
        return
    except Exception as e:  # a bug in one task must not take the worker down
        st.finish_task(tid, "error", None)
        st.event(cfg.run_id, "task_error", {"task": tid, "structure": s, "tier": tier,
                                            "error": f"{type(e).__name__}: {e}",
                                            "trace": traceback.format_exc()[-1500:]},
                 agent_id=worker_id)
        return
    finally:
        st.set_agent_state(worker_id, "idle")
    status = {"ok": "done"}.get(ev.status, ev.status)
    st.finish_task(tid, status, ev.eval_id)
    payload = {"task": tid, "eval_id": ev.eval_id, "structure": s, "tier": tier,
               "mode": ev.mode, "cpu_s": round(ev.cpu_s, 1), "est_cpu_s": round(ev.est_cpu_s, 1),
               "n_atoms_sim": desc.n_atoms_sim}
    if ev.status == "ok" and tier == 3:
        from sieve.capture import brief
        st.event(cfg.run_id, "task_done", {**payload, **(brief(ev.result.get("capture")) or {})},
                 agent_id=worker_id)
    elif ev.status == "ok":
        st.event(cfg.run_id, "task_done", {**payload, "uptake": ev.uptake_molkg,
                                           "err": ev.uptake_err}, agent_id=worker_id)
    elif ev.status == "timeout":
        phase = ev.result["phases"][-1]
        st.event(cfg.run_id, "timeout", {**payload, "phase": phase["kind"],
                                         "limit_cpu_s": round(ev.timeout_s, 1),
                                         "killed_by": phase.get("killed_by")},
                 agent_id=worker_id)
    else:
        st.event(cfg.run_id, "task_error", {**payload, "error": ev.result["phases"][-1].get(
            "stderr", "")[-500:]}, agent_id=worker_id)


def worker_main(cfg: WorkerConfig, worker_id: str, stop: mp.Event) -> None:
    threading.Thread(target=_watch_parent, args=(os.getppid(),), daemon=True).start()
    st = Store(cfg.db_path)
    o = make_oracle(cfg)
    st.upsert_agent(cfg.run_id, worker_id, "worker", state="idle")
    st.event(cfg.run_id, "worker_start", {"pid": os.getpid()}, agent_id=worker_id)
    while not stop.is_set():
        task = st.claim_task(cfg.run_id, worker_id)
        if task is None:
            time.sleep(POLL_S)
            continue
        run_task(st, o, cfg, worker_id, task)
    st.set_agent_state(worker_id, "exited")


class InlineExecutor:
    """Drop-in for WorkerPool that runs queued tasks synchronously in this process.

    For replay seeds, where a 'GCMC call' is a draw and processes would only add overhead.
    supervise() drains the queue, so drivers written against WorkerPool work unchanged.
    """

    def __init__(self, cfg: WorkerConfig, n: int | None = None, parent_agent: str | None = None):
        self.cfg, self.n, self.parent_agent = cfg, n or 1, parent_agent
        self.store = Store(cfg.db_path)
        self.oracle = make_oracle(cfg)
        self.restarts = 0

    def start(self) -> None:
        self.store.upsert_agent(self.cfg.run_id, "inline-0", "worker", state="idle")
        self.store.event(self.cfg.run_id, "pool_start", {"workers": 1, "inline": True},
                         agent_id=self.parent_agent)

    def supervise(self) -> list[dict]:
        while (task := self.store.claim_task(self.cfg.run_id, "inline-0")) is not None:
            run_task(self.store, self.oracle, self.cfg, "inline-0", task)
        return []

    def idle(self) -> bool:
        self.supervise()
        return True

    def stop(self, timeout: float = 0.0) -> None:
        self.store.set_agent_state("inline-0", "exited")


class WorkerPool:
    """Owns the worker processes for one run. Call supervise() regularly from the run loop."""

    def __init__(self, cfg: WorkerConfig, n: int | None = None, parent_agent: str | None = None):
        self.cfg = cfg
        self.n = n or default_workers()
        self.parent_agent = parent_agent
        self.ctx = mp.get_context("spawn")
        self.stop_event = self.ctx.Event()
        self.procs: dict[int, tuple[str, mp.Process]] = {}  # slot -> (worker_id, process)
        self.restarts = 0
        self.store = Store(cfg.db_path)

    def _spawn(self, slot: int, worker_id: str) -> None:
        p = self.ctx.Process(target=worker_main, args=(self.cfg, worker_id, self.stop_event),
                             name=worker_id, daemon=True)
        p.start()
        self.procs[slot] = (worker_id, p)

    def start(self) -> None:
        for slot in range(self.n):
            self._spawn(slot, f"worker-{slot}")
        self.store.event(self.cfg.run_id, "pool_start", {"workers": self.n},
                         agent_id=self.parent_agent)

    def supervise(self) -> list[dict]:
        """Restart dead workers; reclaim their in-flight work. Returns what was recovered."""
        recovered = []
        for slot, (wid, p) in list(self.procs.items()):
            if p.is_alive() or self.stop_event.is_set():
                continue
            info = self.store.reclaim_worker(self.cfg.run_id, wid)
            self.store.set_agent_state(wid, "failed")
            self.store.event(self.cfg.run_id, "worker_dead",
                             {"exitcode": p.exitcode, **info}, agent_id=wid,
                             parent_id=self.parent_agent)
            self.restarts += 1
            new_id = f"worker-{slot}.r{self.restarts}"
            self._spawn(slot, new_id)
            self.store.upsert_agent(self.cfg.run_id, new_id, "worker", parent_id=self.parent_agent,
                                    state="recovered")
            self.store.event(self.cfg.run_id, "worker_restart", {"replaces": wid, **info},
                             agent_id=new_id, parent_id=self.parent_agent)
            recovered.append({"dead": wid, "new": new_id, **info})
        return recovered

    def idle(self) -> bool:
        return not self.store.tasks(self.cfg.run_id, "queued") and \
            not self.store.tasks(self.cfg.run_id, "running")

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        deadline = time.time() + timeout
        for _, p in self.procs.values():
            p.join(max(0.0, deadline - time.time()))
        for wid, p in self.procs.values():
            if p.is_alive():
                p.kill()
                p.join()
            self.store.reclaim_worker(self.cfg.run_id, wid)

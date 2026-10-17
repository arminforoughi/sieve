"""SQLite store: the single source of truth for a run. The dashboard reads only this.

Tables (evaluations and timeouts are written by the oracle; everything else here):
  runs         one row per run (arm, config, budget, status)
  rounds       one row per orchestrator round; finished_at set only when the round committed
  agents       one row per agent instance, with model, state and cumulative tokens / cost
  tasks        (structure, tier) jobs queued by the allocator and executed by workers
  events       append-only log: every agent action, decision, veto, timeout, restart
  evaluations  oracle call log; the budget ledger is derived from it
  timeouts     oracle kills

resume(run_id) rebuilds a RunState from these tables alone and reconciles whatever a crash left
half-done: in-flight evaluations become 'lost' and are charged the CPU their heartbeats prove
was spent; their tasks are re-queued. Completed evaluations are never re-run.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sieve import oracle

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    arm          TEXT NOT NULL,          -- sieve | random | descriptor | uniform
    budget_cpu_s REAL NOT NULL,
    config_json  TEXT NOT NULL,
    status       TEXT NOT NULL,          -- running | finished | failed
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    finished_at  REAL
);
CREATE TABLE IF NOT EXISTS rounds (
    run_id       TEXT NOT NULL,
    round_no     INTEGER NOT NULL,
    started_at   REAL NOT NULL,
    finished_at  REAL,                   -- NULL = round did not commit (crash mid-round)
    plan_json    TEXT,
    summary_json TEXT,
    PRIMARY KEY (run_id, round_no)
);
CREATE TABLE IF NOT EXISTS agents (
    agent_id     TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    role         TEXT NOT NULL,          -- orchestrator | scout | allocator | critic | worker
    model        TEXT,
    parent_id    TEXT,
    state        TEXT NOT NULL,          -- idle | working | failed | recovered | exited
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    cost_usd     REAL NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    round_no     INTEGER NOT NULL,
    structure    TEXT NOT NULL,
    tier         INTEGER NOT NULL,
    mode         TEXT NOT NULL DEFAULT 'auto',
    priority     REAL NOT NULL DEFAULT 0,
    reason       TEXT,
    status       TEXT NOT NULL,          -- queued | running | done | timeout | error | skipped
    attempts     INTEGER NOT NULL DEFAULT 0,
    eval_id      TEXT,
    worker_id    TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_run ON tasks(run_id, status);
CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    ts           REAL NOT NULL,
    agent_id     TEXT,
    parent_id    TEXT,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, event_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    con = oracle.connect(db_path)  # WAL, row factory, oracle tables
    con.executescript(SCHEMA)
    return con


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.con = connect(self.db_path)

    # -- runs --------------------------------------------------------------------------------

    def create_run(self, arm: str, budget_cpu_s: float, config: dict,
                   run_id: str | None = None) -> str:
        run_id = run_id or f"{arm}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        now = time.time()
        self.con.execute("INSERT INTO runs VALUES (?,?,?,?, 'running', ?,?, NULL)",
                         (run_id, arm, budget_cpu_s, json.dumps(config), now, now))
        self.event(run_id, "run_start", {"arm": arm, "budget_cpu_s": budget_cpu_s,
                                         "config": config})
        return run_id

    def finish_run(self, run_id: str, status: str = "finished", summary: dict | None = None) -> None:
        now = time.time()
        self.con.execute("UPDATE runs SET status=?, updated_at=?, finished_at=? WHERE run_id=?",
                         (status, now, now, run_id))
        self.event(run_id, "run_end", {"status": status, **(summary or {})})

    def run(self, run_id: str) -> sqlite3.Row:
        row = self.con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"no run {run_id!r} in {self.db_path}")
        return row

    # -- events ------------------------------------------------------------------------------

    def event(self, run_id: str, type: str, payload: dict | None = None,
              agent_id: str | None = None, parent_id: str | None = None) -> int:
        cur = self.con.execute(
            "INSERT INTO events (run_id, ts, agent_id, parent_id, type, payload_json) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, time.time(), agent_id, parent_id, type, json.dumps(payload or {}, default=str)))
        return cur.lastrowid

    def events(self, run_id: str, after_id: int = 0, limit: int = 1000) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM events WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT ?",
            (run_id, after_id, limit)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in rows]

    # -- rounds ------------------------------------------------------------------------------

    def start_round(self, run_id: str, round_no: int, plan: dict | None = None) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO rounds (run_id, round_no, started_at, plan_json) VALUES (?,?,?,?)",
            (run_id, round_no, time.time(), json.dumps(plan or {})))
        self.event(run_id, "round_start", {"round": round_no, **(plan or {})})

    def finish_round(self, run_id: str, round_no: int, summary: dict) -> None:
        self.con.execute(
            "UPDATE rounds SET finished_at=?, summary_json=? WHERE run_id=? AND round_no=?",
            (time.time(), json.dumps(summary), run_id, round_no))
        self.event(run_id, "round_end", {"round": round_no, **summary})

    # -- agents ------------------------------------------------------------------------------

    def upsert_agent(self, run_id: str, agent_id: str, role: str, model: str | None = None,
                     parent_id: str | None = None, state: str = "idle") -> None:
        now = time.time()
        self.con.execute(
            "INSERT INTO agents (agent_id, run_id, role, model, parent_id, state, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET "
            "state=excluded.state, updated_at=excluded.updated_at",
            (agent_id, run_id, role, model, parent_id, state, now, now))

    def set_agent_state(self, agent_id: str, state: str) -> None:
        self.con.execute("UPDATE agents SET state=?, updated_at=? WHERE agent_id=?",
                         (state, time.time(), agent_id))

    def add_usage(self, agent_id: str, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        self.con.execute(
            "UPDATE agents SET tokens_in=tokens_in+?, tokens_out=tokens_out+?, cost_usd=cost_usd+?, "
            "updated_at=? WHERE agent_id=?", (tokens_in, tokens_out, cost_usd, time.time(), agent_id))

    # -- tasks -------------------------------------------------------------------------------

    def queue_tasks(self, run_id: str, round_no: int, jobs: list[dict]) -> list[str]:
        """jobs: [{structure, tier, mode?, priority?, reason?}]"""
        now, ids = time.time(), []
        self.con.execute("BEGIN IMMEDIATE")
        try:
            for j in jobs:
                tid = uuid.uuid4().hex
                ids.append(tid)
                self.con.execute(
                    "INSERT INTO tasks (task_id, run_id, round_no, structure, tier, mode, priority, "
                    "reason, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, 'queued', ?,?)",
                    (tid, run_id, round_no, j["structure"], j["tier"], j.get("mode", "auto"),
                     j.get("priority", 0.0), j.get("reason"), now, now))
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return ids

    def claim_task(self, run_id: str, worker_id: str) -> sqlite3.Row | None:
        self.con.execute("BEGIN IMMEDIATE")
        try:
            row = self.con.execute(
                "SELECT * FROM tasks WHERE run_id=? AND status='queued' "
                "ORDER BY round_no, priority DESC, created_at LIMIT 1", (run_id,)).fetchone()
            if row:
                self.con.execute(
                    "UPDATE tasks SET status='running', worker_id=?, attempts=attempts+1, "
                    "updated_at=? WHERE task_id=?", (worker_id, time.time(), row["task_id"]))
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return row

    def finish_task(self, task_id: str, status: str, eval_id: str | None) -> None:
        self.con.execute("UPDATE tasks SET status=?, eval_id=?, updated_at=? WHERE task_id=?",
                         (status, eval_id, time.time(), task_id))

    def reclaim_worker(self, run_id: str, worker_id: str | None = None) -> dict:
        """Repair work left in flight by a dead worker (or by every worker, if worker_id is None).

        Its 'running' evaluations become 'lost', charged the child CPU their last heartbeat
        recorded; its 'running' tasks go back to 'queued'.
        """
        w_sql, w_arg = ("AND worker_id=?", (worker_id,)) if worker_id else ("", ())
        out = {"lost_evaluations": 0, "lost_cpu_s": 0.0, "requeued_tasks": 0}
        self.con.execute("BEGIN IMMEDIATE")
        try:
            for r in self.con.execute(f"SELECT eval_id, heartbeat_cpu_s FROM evaluations WHERE "
                                      f"run_id=? AND status='running' {w_sql}",
                                      (run_id, *w_arg)).fetchall():
                cpu = max(0.0, r["heartbeat_cpu_s"] or 0.0)
                self.con.execute("UPDATE evaluations SET status='lost', cpu_s=?, finished_at=? "
                                 "WHERE eval_id=?", (cpu, time.time(), r["eval_id"]))
                out["lost_evaluations"] += 1
                out["lost_cpu_s"] += cpu
            cur = self.con.execute(f"UPDATE tasks SET status='queued', worker_id=NULL, updated_at=? "
                                   f"WHERE run_id=? AND status='running' {w_sql}",
                                   (time.time(), run_id, *w_arg))
            out["requeued_tasks"] = cur.rowcount
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        out["lost_cpu_s"] = round(out["lost_cpu_s"], 2)
        return out

    def tasks(self, run_id: str, status: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM tasks WHERE run_id=?", [run_id]
        if status:
            q += " AND status=?"
            args.append(status)
        return self.con.execute(q + " ORDER BY created_at", args).fetchall()


# ---------------------------------------------------------------------------------- resume

@dataclass
class StructureEstimate:
    structure: str
    tier: int                  # highest tier with an ok evaluation
    uptake: float              # inverse-variance mean over evaluations at that tier
    err: float
    n_evals: int
    tiers: dict = field(default_factory=dict)  # tier -> [(uptake, err)]


@dataclass
class RunState:
    run_id: str
    arm: str
    status: str
    config: dict
    budget_cpu_s: float
    spent_cpu_s: float
    last_completed_round: int   # -1 if none
    estimates: dict[str, StructureEstimate]
    described: set[str]         # structures with a tier-0 row
    queued: list[sqlite3.Row]   # tasks still to run (incl. re-queued after a crash)
    agents: list[sqlite3.Row]
    n_events: int
    reconciled: dict            # what resume() repaired

    @property
    def remaining_cpu_s(self) -> float:
        return self.budget_cpu_s - self.spent_cpu_s


def estimates_from_evaluations(con: sqlite3.Connection, run_id: str) -> dict[str, StructureEstimate]:
    per: dict[str, dict[int, list[tuple[float, float]]]] = {}
    for r in con.execute("SELECT structure, tier, uptake_molkg, uptake_err FROM evaluations "
                         "WHERE run_id=? AND tier>0 AND status='ok'", (run_id,)):
        per.setdefault(r["structure"], {}).setdefault(r["tier"], []).append(
            (r["uptake_molkg"], r["uptake_err"]))
    out = {}
    for s, tiers in per.items():
        top = max(tiers)
        vals = tiers[top]
        w = [1.0 / max(e, 1e-6) ** 2 for _, e in vals]
        mean = sum(wi * u for wi, (u, _) in zip(w, vals)) / sum(w)
        out[s] = StructureEstimate(s, top, mean, (1.0 / sum(w)) ** 0.5, len(vals), tiers)
    return out


def resume(db_path: str | Path, run_id: str, reconcile: bool = True) -> RunState:
    """Rebuild the full state of a run from the tables alone.

    With reconcile=True (what `sieve resume` does), repair what a crash left behind:
      - evaluations still 'running' -> 'lost', charged the child CPU-seconds their last heartbeat
        recorded (heartbeats every 5 s, so at most ~5 CPU-s per lost job goes uncharged)
      - tasks still 'running' -> 'queued' again
      - a round with no finished_at is reopened (its done tasks are kept)
    """
    st = Store(db_path)
    con = st.con
    run = st.run(run_id)
    fixed: dict = {"lost_evaluations": 0, "lost_cpu_s": 0.0, "requeued_tasks": 0,
                   "open_round": None}
    if reconcile:
        fixed.update(st.reclaim_worker(run_id))
        fixed["open_round"] = con.execute("SELECT MIN(round_no) FROM rounds WHERE run_id=? AND "
                                          "finished_at IS NULL", (run_id,)).fetchone()[0]
        con.execute("UPDATE runs SET status='running', updated_at=? WHERE run_id=?",
                    (time.time(), run_id))
        st.event(run_id, "run_resume", fixed)

    last = con.execute("SELECT MAX(round_no) FROM rounds WHERE run_id=? AND finished_at IS NOT NULL",
                       (run_id,)).fetchone()[0]
    spent = con.execute(
        "SELECT COALESCE(SUM(CASE WHEN status='running' THEN est_cpu_s ELSE cpu_s END), 0) "
        "FROM evaluations WHERE run_id=?", (run_id,)).fetchone()[0]
    described = {r[0] for r in con.execute(
        "SELECT DISTINCT structure FROM evaluations WHERE run_id=? AND tier=0", (run_id,))}
    return RunState(
        run_id=run_id, arm=run["arm"], status=run["status"],
        config=json.loads(run["config_json"]), budget_cpu_s=run["budget_cpu_s"],
        spent_cpu_s=float(spent), last_completed_round=-1 if last is None else last,
        estimates=estimates_from_evaluations(con, run_id), described=described,
        queued=st.tasks(run_id, "queued"),
        agents=con.execute("SELECT * FROM agents WHERE run_id=?", (run_id,)).fetchall(),
        n_events=con.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (run_id,)).fetchone()[0],
        reconciled=fixed)

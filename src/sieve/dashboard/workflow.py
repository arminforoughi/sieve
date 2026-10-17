"""The "How it works" page: the agent workflow as one figure, with live counts for one run.

Read-only. Each stage reports what happened in the chosen run; a stage that run never used
(an older agent design, an objective it did not have) reports None and the page says so.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _ro(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _one(con, sql: str, *args) -> int:
    r = con.execute(sql, args).fetchone()
    return int(r[0] or 0) if r else 0


def stages(db: str, run_id: str) -> dict:
    con = _ro(db)
    row = con.execute("SELECT arm, status, budget_cpu_s, config_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return {"run": None}
    cfg = json.loads(row["config_json"] or "{}")
    models = dict(cfg.get("role_models") or {})
    for (pl,) in con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='llm_usage'", (run_id,)):
        p = json.loads(pl)  # what each role actually called, for runs that predate role_models
        if p.get("role") and p.get("model"):
            models.setdefault(p["role"], p["model"])
    ev = lambda t, like=None: _one(con, "SELECT COUNT(*) FROM events WHERE run_id=? AND type=?" + (  # noqa: E731
        " AND agent_id LIKE ?" if like else ""), run_id, t, *([like] if like else []))
    tasks = {(r["tier"], r["status"]): r["n"] for r in con.execute(
        "SELECT tier, status, COUNT(*) n FROM tasks WHERE run_id=? GROUP BY tier, status", (run_id,))}
    t = lambda tier, st="done": tasks.get((tier, st), 0)  # noqa: E731
    rand = _one(con, "SELECT COUNT(*) FROM tasks WHERE run_id=? AND reason LIKE 'random slice%'", run_id)
    rec = con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='recommend' ORDER BY ts DESC LIMIT 1",
                      (run_id,)).fetchone()
    rec = json.loads(rec[0]) if rec else None
    spent = _one(con, "SELECT COALESCE(SUM(cpu_s), 0) FROM evaluations WHERE run_id=?", run_id)
    agentic = (row["arm"] or "").startswith("sieve")
    return {
        "run": {"run_id": run_id, "arm": row["arm"], "status": row["status"], "agentic": agentic,
                "budget_h": round(row["budget_cpu_s"] / 3600, 2), "spent_h": round(spent / 3600, 2),
                "design": cfg.get("agent_design"), "policy": cfg.get("policy") or {},
                "objective": cfg.get("objective", "uptake"), "pool": cfg.get("pool")},
        "models": models,
        "describe": {"structures": t(0)},
        "scout": {"calls": ev("agent_finish", "scout%"), "sub_scouts": _one(
            con, "SELECT COUNT(DISTINCT agent_id) FROM events WHERE run_id=? AND agent_id LIKE 'scout/%'", run_id)} if agentic else None,
        "allocate": {"calls": ev("agent_finish", "allocator%"), "random_slice": rand,
                     "rounds": ev("round_end")} if agentic else {"rounds": ev("round_end")},
        "simulate": {"tier1": t(1), "tier2": t(2), "tier3": t(3), "timeouts": t(1, "timeout") + t(2, "timeout") + t(3, "timeout")},
        "critique": {"calls": ev("agent_finish", "critic%"), "vetoes": ev("veto"),
                     "sub_critics": _one(con, "SELECT COUNT(DISTINCT agent_id) FROM events WHERE run_id=? AND agent_id LIKE 'critic/%'", run_id),
                     "disorder_flags": ev("disorder_artifact")} if agentic else None,
        "recommend": {"structure": rec.get("structure"), "uptake": rec.get("uptake"), "tier": rec.get("tier")} if rec else None,
        "llm_calls": ev("llm_usage"),
    }


def workflow(db: str, run_id: str | None) -> dict:
    from sieve.dashboard import validation
    con = _ro(db)
    if not run_id:
        r = con.execute("SELECT run_id FROM runs WHERE arm LIKE 'sieve%' ORDER BY created_at DESC LIMIT 1").fetchone()
        run_id = r[0] if r else None
    out = stages(db, run_id) if run_id else {"run": None}
    try:
        ch = validation.chain(db, run_id)
        out["verify"] = {"referee": ch.get("referee"), "review": ch.get("review")}
    except Exception as e:  # noqa: BLE001  verification data is optional for this page
        out["verify"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    try:
        from sieve import disorder
        d = disorder.load()
        out["verify"]["disorder"] = {"scanned": len(d), "disordered": sum(1 for r in d.values() if r.get("disordered"))}
    except Exception:  # noqa: BLE001
        pass
    return out

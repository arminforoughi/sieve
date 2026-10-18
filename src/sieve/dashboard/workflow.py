"""The "How it works" page: the agent workflow as one figure, with live counts for one run.

Read-only. Each stage reports what happened in the chosen run; a stage that run never used
(an older agent design, an objective it did not have) reports None and the page says so.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_CACHE: dict[str, tuple[float, dict]] = {}
_BUSY: set[str] = set()
_TTL_S = 600.0


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


def _delegations(db: Path, run_id: str | None = None) -> dict:
    """role -> depth -> {"spawns": delegate events, "children": agents spawned}."""
    if not Path(db).exists():
        return {}
    con = _ro(str(db))
    q = ("SELECT json_extract(payload_json,'$.role'), json_extract(payload_json,'$.depth'), COUNT(*), "
         "SUM(json_extract(payload_json,'$.n')) FROM events WHERE type='delegate'"
         + (" AND run_id=?" if run_id else "") + " GROUP BY 1, 2")
    out: dict = {}
    for role, depth, n, kids in con.execute(q, (run_id,) if run_id else ()):
        out.setdefault(role or "?", {})[str(depth or 1)] = {"spawns": n, "children": int(kids or 0)}
    return out


def _cached_bg(key: str, fn) -> dict | None:
    """The arena DB holds millions of events, so its scans run in a background thread and the page
    shows the last result (None until the first one lands, refreshed every _TTL_S)."""
    hit = _CACHE.get(key)
    if (hit is None or time.time() - hit[0] > _TTL_S) and key not in _BUSY:
        _BUSY.add(key)

        def work():
            try:
                _CACHE[key] = (time.time(), fn())
            finally:
                _BUSY.discard(key)
        threading.Thread(target=work, daemon=True).start()
    return hit[1] if hit else None


def recursion(db: str, run_id: str | None) -> dict:
    """How deep the agent trees actually went: this run, all arena benchmark runs, peer reviews."""
    from sieve.arena_eval import REPLAY_DB
    from sieve.agents.base import MAX_DEPTH

    root = Path(db).resolve().parent
    arena_runs = (_cached_bg("arena_runs", lambda: {"n": _one(_ro(str(REPLAY_DB)), "SELECT COUNT(*) FROM runs WHERE arm LIKE 'sieve-v%'")})
                  or {}).get("n") if Path(REPLAY_DB).exists() else 0
    return {"max_depth": MAX_DEPTH,
            "this_run": _delegations(Path(db), run_id) if run_id else {},
            "benchmark": _cached_bg("arena", lambda: _delegations(Path(REPLAY_DB))), "benchmark_runs": arena_runs,
            "review": _delegations(root / "science" / "reviews.db")}


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
    try:
        out["recursion"] = recursion(db, run_id)
    except Exception as e:  # noqa: BLE001
        out["recursion"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    return out

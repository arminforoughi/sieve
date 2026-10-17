"""Read-only views of the validation side of SIEVE, for the Agents page's "Validation" view.

  chain    the end-to-end validation chain with live counts: screening agents -> critic vetoes ->
           tier-2 confirmations -> referee (independent tier-2 re-measurement of every finished
           arm's top 10, run "referee" in data/runs.db) -> peer reviewer (claims checked per
           structure, from data/science/review-<name>.json)
  reviews  every finished peer review: tally, claim counts, models
  review   one review: the final claim tree plus its agent events from data/science/reviews.db,
           in time order, so the page can replay the recursion

Nothing here writes: every SQLite connection is opened mode=ro, and the reviewer's own get()
(which starts a review when none exists) is never called.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from sieve.oracle import PROJECT_ROOT

SCI_DIR = PROJECT_ROOT / "data" / "science"
REVIEW_DB = SCI_DIR / "reviews.db"
VERDICTS = ("supported", "partly", "refuted", "unverifiable")
REVIEW_EVENTS = ("run_start", "run_end", "agent_start", "agent_finish", "agent_fail", "agent_restart",
                 "agent_exit", "delegate", "llm_usage", "discovery")
# claims about the flue-gas capture metrics the reviewer checks since the capture props landed
CAPTURE = re.compile(r"selectiv|working capacit|temperature[- ]swing|vacuum[- ]swing|heat of adsorption|"
                     r"\bq_?st\b|isosteric|regenerat|henry", re.I)


def _ro(db: Path) -> sqlite3.Connection | None:
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.OperationalError:
        return None


def _walk(nodes: list[dict]):
    for n in nodes:
        yield n
        yield from _walk(n.get("children") or [])


def _load(name: str) -> dict | None:
    f = SCI_DIR / f"review-{name}.json"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", name) or not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except ValueError:
        return None


def _brief(name: str, r: dict) -> dict:
    nodes = list(_walk(r.get("claims") or []))
    cap = [n for n in (r.get("claims") or []) if CAPTURE.search(n.get("claim") or "")]
    return {"name": name, "tally": r.get("tally") or {}, "models": r.get("models") or {},
            "n_claims": len(r.get("claims") or []), "n_nodes": len(nodes),
            "n_split": sum(bool(n.get("children")) for n in nodes),
            "max_depth": max((n["id"].count("/") for n in nodes if n.get("id")), default=0),
            "grounded": sum(len(n.get("grounding") or []) for n in nodes),
            "grounding_used": sum(bool(g.get("used")) for n in nodes for g in (n.get("grounding") or [])),
            "capture": {"n": len(cap), **{v: sum(n.get("verdict") == v for n in cap) for v in VERDICTS}},
            "updated_at": (SCI_DIR / f"review-{name}.json").stat().st_mtime}


def reviews() -> list[dict]:
    """Every structure with a finished review, in the Science page's order (references after the pool)."""
    out = []
    for f in sorted(SCI_DIR.glob("review-*.json")):
        if f.name.endswith(".small.json"):
            continue
        name = f.name[len("review-"):-len(".json")]
        r = _load(name)
        if r:
            out.append(_brief(name, r))
    first = {"ZADREE_clean": 0, "QIPROY_clean": 1, "Cu-BTC": 2, "MgMOF-74-VASP": 3}
    return sorted(out, key=lambda b: (first.get(b["name"], 9), b["name"]))


def review(name: str) -> dict:
    r = _load(name)
    if r is None:
        return {"name": name, "status": "missing"}
    evs, run = [], None
    con = _ro(REVIEW_DB)
    if con is not None:
        try:
            row = con.execute("SELECT status, created_at, finished_at FROM runs WHERE run_id=?",
                              (f"review-{name}",)).fetchone()
            run = dict(row) if row else None
            q = ",".join("?" * len(REVIEW_EVENTS))
            for e in con.execute(f"SELECT event_id, ts, agent_id, parent_id, type, payload_json FROM events "
                                 f"WHERE run_id=? AND type IN ({q}) ORDER BY ts, event_id",
                                 (f"review-{name}", *REVIEW_EVENTS)):
                p = json.loads(e["payload_json"])
                d = p.get("decision") or {}
                evs.append({"id": e["event_id"], "ts": e["ts"], "agent": e["agent_id"], "parent": e["parent_id"],
                            "type": e["type"], "text": str(p.get("text") or p.get("error") or p.get("headline") or "")[:200],
                            "model": p.get("model"), "verdict": d.get("verdict"), "n": p.get("n"),
                            "tokens": (p.get("tokens_in") or 0) + (p.get("tokens_out") or 0) or None,
                            "latency_s": p.get("latency_s"), "retry": p.get("retry")})
        except sqlite3.OperationalError:
            pass
        finally:
            con.close()
    usage: dict[str, dict] = {}
    for e in evs:
        if e["type"] == "llm_usage" and e["model"]:
            u = usage.setdefault(e["model"], {"model": e["model"], "calls": 0, "tokens": 0, "agents": set()})
            u["calls"] += 1
            u["tokens"] += e["tokens"] or 0
            u["agents"].add(e["agent"])
    for u in usage.values():
        u["agents"] = len(u["agents"])
    # the events belong to the latest run of this review; the tree to the latest finished one
    ids = {n["id"] for n in _walk(r.get("claims") or [])}
    seen = {e["agent"] for e in evs if e["type"] == "agent_start"}
    return {"status": "ready", "name": name, **{k: r.get(k) for k in ("structure", "analyst_writeup", "claims", "tally",
                                                                     "assessment", "would_change_our_mind", "models")},
            "brief": _brief(name, r), "run": run, "events": evs, "usage": sorted(usage.values(), key=lambda u: -u["calls"]),
            "events_match_tree": bool(ids) and ids <= seen}


def _estimates(con: sqlite3.Connection, run_id: str):
    from sieve.store import estimates_from_evaluations
    return estimates_from_evaluations(con, run_id)


def chain(db: str, run_id: str | None = None) -> dict:
    """Live counts for each link of the validation chain."""
    out: dict = {"run": None, "screening": {}, "critic": {}, "tier2": {}, "referee": {}, "review": {}}
    con = _ro(Path(db))
    if con is not None:
        try:
            runs = [dict(r) for r in con.execute("SELECT run_id, arm, status FROM runs ORDER BY created_at DESC")]
            sieve = [r for r in runs if r["arm"] == "sieve"]
            run_id = run_id if any(r["run_id"] == run_id and r["arm"] == "sieve" for r in runs) else \
                (sieve[0]["run_id"] if sieve else None)
            out["run"] = run_id
            if run_id:
                ag = [r[0] for r in con.execute("SELECT DISTINCT agent_id FROM events WHERE run_id=? AND agent_id IS NOT NULL",
                                                (run_id,))]
                team = [a for a in ag if not a.startswith(("worker", "inline"))]
                calls = con.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND type='llm_usage'", (run_id,)).fetchone()[0]
                t1 = con.execute("SELECT COUNT(*), COUNT(DISTINCT structure) FROM evaluations WHERE run_id=? AND tier=1 "
                                 "AND status='ok'", (run_id,)).fetchone()
                out["screening"] = {"agents": len(team), "top_agents": len([a for a in team if "/" not in a]),
                                    "sub_agents": len([a for a in team if "/" in a]), "llm_calls": calls,
                                    "tier1": t1[0], "tier1_structures": t1[1]}
                vet = [json.loads(r[0]) for r in con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='veto'",
                                                             (run_id,))]
                reasons: dict[str, int] = {}
                for v in vet:
                    k = str(v.get("reason") or "?")[:40]
                    reasons[k] = reasons.get(k, 0) + 1
                out["critic"] = {"vetoes": len(vet), "structures": len({v.get("structure") for v in vet}),
                                 "reasons": sorted(([k, n] for k, n in reasons.items()), key=lambda x: -x[1])[:3]}
                t2 = con.execute("SELECT COUNT(*), COUNT(DISTINCT structure) FROM evaluations WHERE run_id=? AND tier=2 "
                                 "AND status='ok'", (run_id,)).fetchone()
                rec = con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='recommend' "
                                  "ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone()
                out["tier2"] = {"runs": t2[0], "structures": t2[1],
                                "recommended": (lambda p: {"structure": p.get("structure"), "uptake": p.get("uptake"),
                                                           "err": p.get("err")})(json.loads(rec[0])) if rec else None}
            # referee: union of every finished arm's top 10, re-measured independently at tier 2
            arms = {}
            for r in runs:
                if r["status"] == "finished" and r["arm"] != "referee":
                    est = _estimates(con, r["run_id"])
                    arms[r["run_id"]] = sorted(est.values(), key=lambda e: e.uptake, reverse=True)[:10]
            union = {e.structure for picks in arms.values() for e in picks}
            ref = _estimates(con, "referee")
            st = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM evaluations WHERE run_id='referee' "
                                                   "AND tier=2 GROUP BY status")}
            top = sorted(ref.values(), key=lambda e: e.uptake, reverse=True)
            ref_top = {e.structure for e in top[:10]}
            rstat = next((r["status"] for r in runs if r["run_id"] == "referee"), None)
            out["referee"] = {
                "status": rstat, "union": len(union), "measured": len(union & set(ref)),
                "measurements": st.get("ok", 0), "running": st.get("running", 0), "lost": st.get("lost", 0),
                "replicated": sum(e.n_evals >= 2 for e in ref.values()),
                "best": {"structure": top[0].structure, "uptake": top[0].uptake, "err": top[0].err} if top else None,
                "arms": [{"run": rid, "pick1": p[0].structure if p else None,
                          "claimed": p[0].uptake if p else None, "claimed_tier": p[0].tier if p else None,
                          "pick1_referee": ref[p[0].structure].uptake if p and p[0].structure in ref else None,
                          "hits": len(ref_top & {e.structure for e in p}) if len(ref) >= 10 else None,
                          "checked": sum(e.structure in ref for e in p), "k": len(p)}
                         for rid, p in sorted(arms.items())]}
        except sqlite3.OperationalError as e:
            out["error"] = str(e)
        finally:
            con.close()
    rv = reviews()
    tally = {v: sum((b["tally"] or {}).get(v, 0) for b in rv) for v in VERDICTS}
    cap = {k: sum(b["capture"].get(k, 0) for b in rv) for k in ("n", *VERDICTS)}
    out["review"] = {"structures": len(rv), "claims": sum(b["n_claims"] for b in rv), "nodes": sum(b["n_nodes"] for b in rv),
                     "splits": sum(b["n_split"] for b in rv), "tally": tally, "capture": cap,
                     "models": sorted({m for b in rv for m in (b["models"] or {}).values() if m})}
    return out

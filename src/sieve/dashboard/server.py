"""SIEVE control-room dashboard: FastAPI + server-sent events, vanilla JS front end.

Everything shown is computed from the run database as of a clock time T:
  live         T = now
  replay       T = run start + 20 x (time since the replay began)    (--demo-replay RUN_ID)
so an empty database, a run in progress, a crashed run and a replay all render through the
same code. Once a second the stream sends a `frame` (vitals, worker slots, agents, curves)
and the `events` rows with ts <= T that the client has not seen yet.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).parent / "static"
REPLAY_SPEED = 20.0
FRAME_S = 1.0
ARMS = ("random", "descriptor", "uniform", "sieve")


def _con(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _has(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone() is not None


def list_runs(db: str) -> list[dict]:
    try:
        con = _con(db)
    except sqlite3.OperationalError:
        return []
    if not _has(con, "runs"):
        return []
    return [dict(r) for r in con.execute(
        "SELECT run_id, arm, status, budget_cpu_s, created_at, finished_at FROM runs "
        "ORDER BY created_at DESC")]


def curve(con: sqlite3.Connection, run_id: str, T: float) -> list[list[float]]:
    """[(CPU-hours spent, best uptake so far)] at each improvement, from the ledger."""
    pts, spent, best = [], 0.0, None
    for r in con.execute(
            "SELECT tier, status, cpu_s, uptake_molkg FROM evaluations WHERE run_id=? AND "
            "status NOT IN ('running') AND finished_at <= ? ORDER BY finished_at",
            (run_id, T)):
        spent += r["cpu_s"] or 0.0
        if r["status"] == "ok" and r["tier"] > 0 and (best is None or r["uptake_molkg"] > best):
            best = r["uptake_molkg"]
            pts.append([round(spent / 3600, 4), round(best, 4)])
    if best is not None:
        pts.append([round(spent / 3600, 4), round(best, 4)])
    return pts


def frame(db: str, run_id: str | None, T: float) -> dict:
    out: dict = {"T": T, "runs": list_runs(db), "run": None}
    if not run_id:
        return out
    con = _con(db)
    run = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        return out
    t_end = run["finished_at"] if run["finished_at"] and run["finished_at"] <= T else None
    out["run"] = {
        "run_id": run_id, "arm": run["arm"], "status": run["status"] if t_end else "running",
        "budget_cpu_s": run["budget_cpu_s"], "created_at": run["created_at"],
        "elapsed_s": (t_end or T) - run["created_at"], "finished": bool(t_end),
    }
    q = lambda sql, *a: con.execute(sql, (run_id, *a))  # noqa: E731
    spent = q("SELECT COALESCE(SUM(cpu_s),0) FROM evaluations WHERE run_id=? AND "
              "status!='running' AND finished_at<=?", T).fetchone()[0]
    inflight = q("SELECT COALESCE(SUM(est_cpu_s),0) FROM evaluations WHERE run_id=? AND "
                 "started_at<=? AND (finished_at IS NULL OR finished_at>?)", T, T).fetchone()[0]
    counts = {r[0]: r[1] for r in q("SELECT tier, COUNT(*) FROM evaluations WHERE run_id=? AND "
                                    "status='ok' AND finished_at<=? GROUP BY tier", T)}
    timeouts = q("SELECT COUNT(*) FROM timeouts WHERE run_id=? AND killed_at<=?", T).fetchone()[0]
    lost = q("SELECT COUNT(*) FROM evaluations WHERE run_id=? AND status='lost' AND "
             "finished_at<=?", T).fetchone()[0]
    ev_counts = {r[0]: r[1] for r in q(
        "SELECT type, COUNT(*) FROM events WHERE run_id=? AND ts<=? GROUP BY type", T)}
    best = q("SELECT structure, tier, uptake_molkg, uptake_err FROM evaluations WHERE run_id=? AND "
             "status='ok' AND tier>0 AND finished_at<=? ORDER BY uptake_molkg DESC LIMIT 1",
             T).fetchone()
    best_t2 = q("SELECT structure, uptake_molkg, uptake_err FROM evaluations WHERE run_id=? AND "
                "status='ok' AND tier=2 AND finished_at<=? ORDER BY uptake_molkg DESC LIMIT 1",
                T).fetchone()
    spend = [dict(r) for r in q(
        "SELECT json_extract(payload_json,'$.model') AS model, "
        "SUM(json_extract(payload_json,'$.cost_usd')) AS usd, "
        "SUM(json_extract(payload_json,'$.tokens_in') + json_extract(payload_json,'$.tokens_out')) "
        "AS tokens FROM events WHERE run_id=? AND type='llm_usage' AND ts<=? GROUP BY model", T)]
    out["vitals"] = {
        "spent_cpu_s": spent, "inflight_cpu_s": inflight,
        "interventions": ev_counts.get("intervention", 0),
        "best": dict(best) if best else None, "best_t2": dict(best_t2) if best_t2 else None,
        "counts": {"t0": counts.get(0, 0), "t1": counts.get(1, 0), "t2": counts.get(2, 0)},
        "timeouts": timeouts, "lost": lost,
        "worker_restarts": ev_counts.get("worker_restart", 0),
        "resumes": ev_counts.get("run_resume", 0), "vetoes": ev_counts.get("veto", 0),
        "spend": spend,
    }

    # worker slots: the latest task_start per worker, and whether it has finished by T
    workers = {}
    # only recent history matters for the slots (a job never outlives its 15-min CPU cap by much)
    for r in q("SELECT agent_id, ts, type, payload_json FROM events WHERE run_id=? AND ts<=? AND ts>? AND "
               "type IN ('worker_start','task_start','task_done','timeout','task_error',"
               "'budget_refused','worker_dead','worker_restart') ORDER BY event_id", T,
               (q("SELECT MAX(ts) FROM events WHERE run_id=? AND ts<=?", T).fetchone()[0] or T) - 1800):
        p = json.loads(r["payload_json"])
        wid = r["agent_id"]
        slot = wid.split(".")[0] if wid else None
        if slot is None:
            continue
        w = workers.setdefault(slot, {"slot": slot})
        w["worker_id"] = wid
        if r["type"] == "task_start":
            if w.get("last"):
                w["prev"] = {"task": w.get("task"), "structure": w.get("last_structure"),
                             "outcome": w.get("last"), "limit_cpu_s": w.get("limit_cpu_s"),
                             "ended": w.get("ended")}
            w.update(state="running", structure=p["structure"], tier=p["tier"], mode=p["mode"],
                     started=r["ts"], est_cpu_s=p.get("est_cpu_s"),
                     limit_cpu_s=p.get("limit_cpu_s"), task=p["task"])
        elif r["type"] in ("task_done", "timeout", "task_error", "budget_refused"):
            w.update(state={"task_done": "idle", "timeout": "timeout"}.get(r["type"], "idle"),
                     ended=r["ts"], last=r["type"], last_structure=p.get("structure"),
                     last_cpu_s=p.get("cpu_s"))
        elif r["type"] == "worker_dead":
            w.update(state="dead", ended=r["ts"])
        elif r["type"] in ("worker_start", "worker_restart"):
            w.setdefault("state", "idle")
            if r["type"] == "worker_restart":
                w.update(state="idle", restarted=r["ts"])
    if t_end:  # a finished run has no live failures: old timeouts are history, not alarms
        for w in workers.values():
            if w.get("state") in ("timeout", "running", "dead"):
                w["state"] = "idle"
    # CPU progress of each running job: live from heartbeats, in replay by interpolating the
    # job's final CPU over its wall time (the limit is CPU time, so the bar must be too)
    for r in q("SELECT worker_id, started_at, finished_at, cpu_s, heartbeat_at, heartbeat_cpu_s, "
               "timeout_s FROM evaluations WHERE run_id=? AND tier>0 AND started_at<=? AND "
               "(finished_at IS NULL OR finished_at>?)", T, T):
        slot = (r["worker_id"] or "").split(".")[0]
        if slot not in workers:
            continue
        if r["finished_at"]:
            frac = (T - r["started_at"]) / max(1e-6, r["finished_at"] - r["started_at"])
            cpu_now = (r["cpu_s"] or 0.0) * min(1.0, frac)
        else:  # live: extrapolate a little past the last heartbeat (every 5 s), never further
            since = max(0.0, T - (r["heartbeat_at"] or r["started_at"]))
            cpu_now = (r["heartbeat_cpu_s"] or 0.0) + 0.6 * min(since, 10.0)
            if since > 15.0:
                workers[slot]["stalled_s"] = round(since)
        workers[slot].update(cpu_now=round(cpu_now, 1), limit_cpu_s=r["timeout_s"])
    out["workers"] = sorted(workers.values(), key=lambda w: int(w["slot"].split("-")[-1])
                            if w["slot"].split("-")[-1].isdigit() else 99)

    agents = {}
    for r in q("SELECT agent_id, parent_id, ts, type, payload_json FROM events WHERE run_id=? AND "
               "ts<=? AND type IN ('agent_start','agent_finish','agent_fail','agent_restart',"
               "'agent_exit') ORDER BY event_id", T):
        p = json.loads(r["payload_json"])
        a = agents.setdefault(r["agent_id"], {"agent_id": r["agent_id"], "role": p.get("role"),
                                              "model": p.get("model"), "parent": r["parent_id"]})
        a["state"] = {"agent_start": "working", "agent_finish": "idle", "agent_fail": "failed",
                      "agent_restart": "recovered", "agent_exit": "exited"}[r["type"]]
        a["since"] = r["ts"]
    out["agents"] = list(agents.values())

    # every structure measured so far (best estimate at its highest tier) for the pool scatter
    measured: dict[str, list] = {}
    for r in q("SELECT structure, tier, uptake_molkg, uptake_err, finished_at FROM evaluations "
               "WHERE run_id=? AND status='ok' AND tier>0 AND finished_at<=? ORDER BY finished_at",
               T):
        m = measured.get(r[0])
        if m is None or r[1] >= m[2]:
            measured[r[0]] = [round(r[2], 4), round(r[3], 4), r[1], round(r[4], 2)]
    out["measured"] = measured
    last = q("SELECT MAX(round_no) FROM tasks WHERE run_id=? AND tier>0 AND created_at<=?",
             T).fetchone()[0]
    out["last_round"] = {"round": last, "picks": [r[0] for r in q(
        "SELECT structure FROM tasks WHERE run_id=? AND round_no=? AND tier>0 AND created_at<=? "
        "ORDER BY priority DESC, created_at", last, T)]} if last is not None else None

    # one row per agent: its latest event, for the prose line; run-level events belong to the
    # orchestrator (a baseline's fixed-policy driver plays that role)
    rows: dict[str, dict] = {}
    for r in q("SELECT e.agent_id, e.ts, e.type, e.payload_json FROM events e JOIN ("
               "  SELECT COALESCE(agent_id, '') AS a, MAX(event_id) AS m FROM events "
               "  WHERE run_id=? AND ts<=? AND type NOT IN ('llm_usage') GROUP BY a"
               ") x ON e.event_id = x.m", T):
        aid = r[0] or "orchestrator"
        if aid.endswith("-driver"):
            aid = "orchestrator"
        prev = rows.get(aid)
        if prev is None or r[1] > prev["ts"]:
            rows[aid] = {"agent": aid, "ts": r[1], "type": r[2], "p": json.loads(r[3])}
    states = {a["agent_id"]: a["state"] for a in agents.values()}
    for w in workers.values():
        states[w["worker_id"]] = {"running": "working", "timeout": "failed", "dead": "failed"
                                  }.get(w.get("state"), "idle")
    for aid, row in rows.items():
        row["state"] = states.get(aid, "done" if t_end else (
            "working" if aid == "orchestrator" else "idle"))
        row["role"] = next((a["role"] for a in agents.values() if a["agent_id"] == aid), None) or (
            "worker" if aid.startswith("worker") else aid)
        row["model"] = next((a["model"] for a in agents.values() if a["agent_id"] == aid), None)
    out["agent_rows"] = sorted((r for r in rows.values() if not r["agent"].startswith(("worker", "inline"))),
                               key=_agent_order)
    # liveness = latest event OR latest heartbeat of a running job (a long tier-2 job can be
    # quiet in the event log for minutes while its heartbeat says it is fine)
    last_ev = q("SELECT MAX(ts) FROM events WHERE run_id=? AND ts<=?", T).fetchone()[0]
    last_hb = q("SELECT MAX(heartbeat_at) FROM evaluations WHERE run_id=? AND status='running' "
                "AND heartbeat_at<=?", T).fetchone()[0]
    alive = max(x for x in (last_ev, last_hb, 0) if x is not None)
    out["stale_s"] = None if (t_end or not alive) else T - alive
    if out["stale_s"] and out["stale_s"] > 60:
        for row in out["agent_rows"]:
            if row["state"] == "working":
                row["state"] = "failed"

    curves = {}
    for arm in ARMS:
        # the selected run for its own arm, else that arm's most recent run (running or not)
        row = con.execute("SELECT run_id FROM runs WHERE arm=? AND created_at<=? "
                          "ORDER BY (run_id=?) DESC, created_at DESC LIMIT 1",
                          (arm, T, run_id)).fetchone()
        if row:
            curves[arm] = {"run_id": row[0], "points": curve(con, row[0], T)}
    out["curves"] = curves
    out["verdict"] = verdict(con, run_id, T) if t_end else None
    out["reference"] = ({"value": out["verdict"]["best"]["value"], "source": out["verdict"]["source"]}
                        if out["verdict"] and out["verdict"]["best"] else None)
    if t_end and not out["reference"] and _has(con, "reference"):
        ref = con.execute("SELECT value, source FROM reference ORDER BY rowid DESC LIMIT 1"
                          ).fetchone()
        out["reference"] = dict(ref) if ref else None
    return out


def _truth_for(con: sqlite3.Connection, run_id: str) -> tuple[dict, str] | tuple[None, None]:
    """True values for scoring a finished run: arena ground truth for arena runs, else the referee."""
    import json as _j

    cfg = _j.loads(con.execute("SELECT config_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
    if cfg.get("candidates") or cfg.get("replay"):
        from sieve.arena import ARENA_DB
        if ARENA_DB.exists():
            a = sqlite3.connect(f"file:{ARENA_DB}?mode=ro", uri=True)
            if _has(a, "truth"):
                return ({r[0]: r[1] for r in a.execute("SELECT structure, uptake FROM truth")},
                        "arena ground truth (tier-2 GCMC on all 292)")
    if _has(con, "referee"):
        vals = {r[0]: r[1] for r in con.execute("SELECT structure, uptake FROM referee")}
        if vals:
            return vals, f"independent tier-2 re-measurement of every arm's top 10 ({len(vals)} structures)"
    return None, None


def verdict(con: sqlite3.Connection, run_id: str, T: float) -> dict | None:
    """Per arm: the structure it would name as winner, what it claimed, and what it really is."""
    from sieve.store import estimates_from_evaluations

    truth, source = _truth_for(con, run_id)
    ranked = sorted(truth, key=truth.get, reverse=True) if truth else []
    rows = []
    for arm in ARMS:
        r = con.execute("SELECT run_id FROM runs WHERE arm=? AND status='finished' AND finished_at<=? "
                        "ORDER BY (run_id=?) DESC, created_at DESC LIMIT 1", (arm, T, run_id)).fetchone()
        if not r:
            continue
        rec = con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='recommend' "
                          "ORDER BY event_id DESC LIMIT 1", (r[0],)).fetchone()
        est = estimates_from_evaluations(con, r[0])
        if rec:
            pick = json.loads(rec[0]).get("structure")
        else:
            pick = max(est, key=lambda s: est[s].uptake) if est else None
        if not pick:
            continue
        e = est.get(pick)
        rows.append({"arm": arm, "run_id": r[0], "structure": pick,
                     "claimed": e.uptake if e else None, "claimed_err": e.err if e else None,
                     "tier": e.tier if e else None,
                     "true": truth.get(pick) if truth else None,
                     "true_rank": ranked.index(pick) + 1 if truth and pick in truth else None})
    seeds = None
    rep = Path(__file__).resolve().parents[3] / "data" / "arena-report.json"
    if rep.exists() and source and source.startswith("arena"):
        d = json.loads(rep.read_text())
        seeds = {arm: a["budgets"].get("1") for arm, a in d["arms"].items()}
    return {"source": source, "n_truth": len(ranked),
            "best": {"structure": ranked[0], "value": truth[ranked[0]]} if ranked else None,
            "rows": rows, "seeds": seeds}


def discoveries(db: str, run_id: str) -> dict:
    """The run's top structures: dossier (features, percentiles, measurements, who picked them) +
    the analyst's write-up when one exists + the true value if the run has finished and truth exists."""
    from sieve.agents.explain import dossier, pool_percentiles
    from sieve.store import Store, estimates_from_evaluations

    st = Store(db)
    con = st.con
    est = estimates_from_evaluations(con, run_id)
    ranked = sorted(est.values(), key=lambda e: e.uptake, reverse=True)[:6]
    notes = {}
    for (p,) in con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='discovery' ORDER BY event_id",
                            (run_id,)):
        d = json.loads(p)
        notes[d["structure"]] = d
    rec = con.execute("SELECT payload_json FROM events WHERE run_id=? AND type='recommend' ORDER BY event_id DESC LIMIT 1",
                      (run_id,)).fetchone()
    rec = json.loads(rec[0])["structure"] if rec else None
    run = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    truth = None
    if run and run["status"] == "finished":
        truth, _ = _truth_for(con, run_id)
    pct = _pct_cache()
    cards = []
    for i, e in enumerate(ranked, 1):
        dos = dossier(st, run_id, e.structure, pct)
        dos.update(rank=i, estimate={"uptake": e.uptake, "err": e.err, "tier": e.tier},
                   note=notes.get(e.structure), recommended=e.structure == rec,
                   true_value=(truth or {}).get(e.structure))
        cards.append(dos)
    return {"run": dict(run) if run else None, "cards": cards, "explained": bool(notes)}


@lru_cache(maxsize=1)
def _pct_cache():
    from sieve.agents.explain import pool_percentiles
    return pool_percentiles()


ROLE_ORDER = {"orchestrator": 0, "scout": 1, "allocator": 2, "critic": 3}


def _agent_order(row: dict) -> tuple:
    aid = row["agent"]
    if aid in ROLE_ORDER:
        return (ROLE_ORDER[aid], aid)
    if row.get("role") in ROLE_ORDER:
        return (ROLE_ORDER[row["role"]], aid)
    tail = aid.split("-")[-1].split(".")[0]
    return (10, int(tail) if tail.isdigit() else 99, aid)


@lru_cache(maxsize=1)
def pool_points() -> dict:
    """Every candidate as (name, LCD, void fraction) for the scatter. Descriptors only."""
    from sieve.oracle import core_descriptors
    from sieve.pool import load_candidates

    core = core_descriptors()
    pts = []
    for c in load_candidates():
        d = core.get(c.name) or {}
        if d.get("LCD") is not None and d.get("AV_VF") is not None:
            pts.append([c.name, round(d["LCD"], 3), round(d["AV_VF"], 4)])
    return {"x": "largest cavity diameter (A)", "y": "void fraction", "points": pts}


@lru_cache(maxsize=48)
def structure_payload(name: str) -> dict:
    """Cartesian atoms (a supercell of ~18 A, <= 2,400 atoms) plus sampled pore-void points."""
    import math
    import warnings

    import numpy as np
    from pymatgen.core import Structure

    from sieve.oracle import PROJECT_ROOT, bundled_cif_dir, raspa_alias, zeolite_cif

    path = PROJECT_ROOT / "data" / "prepared" / f"{raspa_alias(name)}.cif"
    if not path.exists():
        for cand in (PROJECT_ROOT / "data" / "cifs" / f"{name}.cif",
                     bundled_cif_dir() / f"{name}.cif", zeolite_cif(name) or Path("/nonexistent")):
            if cand.exists():
                path = cand
                break
        else:
            return {"name": name, "error": "structure file not found"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = _zeo_structure(path) if zeolite_cif(name) is not None and "prepared" not in path.parts else Structure.from_file(str(path))
    reps = [max(1, min(3, math.ceil(18.0 / x))) for x in s.lattice.abc]
    while len(s) * reps[0] * reps[1] * reps[2] > 2400 and max(reps) > 1:
        reps[reps.index(max(reps))] -= 1
    sc = s * reps
    xyz = sc.cart_coords
    center = sc.lattice.get_cartesian_coords([0.5, 0.5, 0.5])
    xyz = xyz - center
    els = sorted({site.specie.symbol for site in sc})
    rng = np.random.default_rng(0)
    trial = rng.random((2400, 3))
    d = sc.lattice.get_all_distances(trial, sc.frac_coords).min(axis=1)
    keep = trial[d > 3.2][:900]
    void = sc.lattice.get_cartesian_coords(keep) - center if len(keep) else np.zeros((0, 3))
    metals = {"Li", "Na", "K", "Mg", "Ca", "Sr", "Ba", "Al", "Ga", "In", "Sc", "Ti", "V", "Cr",
              "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Zr", "Y", "Cd", "Ag", "La", "Ce", "Eu", "Gd",
              "Tb", "Dy", "Er", "Yb", "Pb", "Bi", "Mo", "W", "Hf", "Nd", "Sm", "Pr"}
    # bonds for legibility: covalent-radius contacts inside the displayed supercell (no wrap)
    from pymatgen.analysis.molecule_structure_comparator import CovalentRadius

    rad = CovalentRadius.radius
    ci, pi, offs, dist = sc.get_neighbor_list(2.9)
    bonds = []
    for i, j, o, d in zip(ci, pi, offs, dist):
        if i < j and not any(o):
            a, b = sc[int(i)].specie.symbol, sc[int(j)].specie.symbol
            if d <= 1.15 * (rad.get(a, 1.5) + rad.get(b, 1.5)):
                bonds.append([int(i), int(j)])
    kind = lambda el: 1 if el in metals else 3 if el == "H" else 0  # noqa: E731
    return {
        "name": name, "elements": els, "n_cell": len(s), "reps": reps,
        "abc": [round(x, 2) for x in s.lattice.abc], "bonds": bonds,
        "atoms": [[round(float(a), 2), round(float(b), 2), round(float(c), 2), els.index(site.specie.symbol),
                   kind(site.specie.symbol)] for (a, b, c), site in zip(xyz, sc)],
        "void": [[round(float(a), 2), round(float(b), 2), round(float(c), 2)] for a, b, c in void],
        "radius": round(float(np.abs(xyz).max()), 2),
    }


def _zeo_structure(path):
    from sieve.oracle import _load_structure
    return _load_structure(path)


@lru_cache(maxsize=48)
def cif_payload(name: str) -> dict:
    """The P1 CIF the simulation used, plus pore-void sample points in the same unit cell."""
    import warnings

    import numpy as np
    from pymatgen.core import Structure

    from sieve.oracle import PROJECT_ROOT, bundled_cif_dir, raspa_alias, zeolite_cif

    path = PROJECT_ROOT / "data" / "prepared" / f"{raspa_alias(name)}.cif"
    for cand in (path, PROJECT_ROOT / "data" / "cifs" / f"{name}.cif", bundled_cif_dir() / f"{name}.cif",
                 zeolite_cif(name) or Path("/nonexistent")):
        if cand.exists():
            path = cand
            break
    else:
        return {"name": name, "error": "structure file not found"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = Structure.from_file(str(path)) if zeolite_cif(name) is None or "prepared" in path.parts else _zeo_structure(path)
    rng = np.random.default_rng(0)
    trial = rng.random((1500, 3))
    d = s.lattice.get_all_distances(trial, s.frac_coords).min(axis=1)
    void = s.lattice.get_cartesian_coords(trial[d > 3.2][:250]) if (d > 3.2).any() else np.zeros((0, 3))
    reps = 2 if len(s) <= 300 else 1
    return {"name": name, "cif": path.read_text(), "n_cell": len(s), "reps": reps,
            "density": round(float(s.density), 4), "volume": round(float(s.volume), 1),
            "elements": sorted({x.specie.symbol for x in s}),
            "lattice": [[round(float(v), 4) for v in row] for row in s.lattice.matrix],
            "void": [[round(float(v), 2) for v in p] for p in void]}


@lru_cache(maxsize=1)
def pool_index() -> dict[str, dict]:
    """Every pool structure's tier-0 descriptors from data/pool.jsonl (read once, ~12k rows)."""
    from sieve.oracle import PROJECT_ROOT

    out: dict[str, dict] = {}
    path = PROJECT_ROOT / "data" / "pool.jsonl"
    if not path.exists():
        return out
    num = lambda x, k=3: None if x is None else round(float(x), k)  # noqa: E731
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            c = d.get("core") or {}
            out[d["structure"]] = {
                "name": d["structure"], "PLD": num(c.get("PLD")), "LCD": num(c.get("LCD")),
                "VF": num(c.get("AV_VF")), "ASA": num(c.get("ASA_m2_g"), 0),
                "density": num(d.get("density")), "metals": d.get("metals") or [],
                "oms": (c.get("Has_OMS") or "").lower().startswith("y"),
                "n_atoms": d.get("n_atoms"), "parse_ok": bool(d.get("parse_ok"))}
    return out


def _science_ready(name: str) -> bool:
    from sieve.oracle import PROJECT_ROOT, raspa_alias
    return (PROJECT_ROOT / "data" / "science" / f"{raspa_alias(name)}.json").exists()


def _props_brief(name: str) -> dict | None:
    """Headline capture properties (selectivity, VSA working capacity, Qst) if computed, else None."""
    from sieve.oracle import PROJECT_ROOT, raspa_alias
    f = PROJECT_ROOT / "data" / "science" / f"{raspa_alias(name)}.props.json"
    if not f.exists():
        return None
    try:
        p = json.loads(f.read_text())
    except ValueError:
        return None
    pick = lambda d: None if not d or d.get("value") is None else {"v": round(d["value"], 4), "e": round(d["err"], 4)}  # noqa: E731
    return {"S": pick(p.get("selectivity")), "wc_vsa": pick((p.get("wc") or {}).get("vsa")),
            "qst": pick((p.get("qst") or {}).get("widom"))}


ZEOLITE_SET = ("MFI", "CHA", "FAU", "LTA", "MOR", "DDR")  # the representative references computed first


def zeolite_hits(q: str) -> list[dict]:
    """IZA-SC zeolite frameworks matching q: "IZA-MFI", "zeolite MFI", "zeolite", "iza", or a code prefix."""
    import re

    from sieve.oracle import ZEOLITE_PREFIX, zeolite_codes
    q = (q or "").strip().upper()
    if not q:
        return []
    word = bool(re.search(r"\b(ZEOLITES?|IZA)\b", q))
    rest = re.sub(r"\b(ZEOLITES?|IZA)\b|[-_\s]", "", q)
    codes = zeolite_codes()
    if rest:
        hits = [c for c in codes if c.startswith(rest)]
    else:
        hits = [c for c in ZEOLITE_SET if c in codes] + [c for c in codes if c not in ZEOLITE_SET] if word else []
    if not word and len(rest) < 2:
        return []
    return [{"name": f"{ZEOLITE_PREFIX}{c}", "zeolite": c, "PLD": None, "LCD": None, "VF": None, "ASA": None,
             "density": None, "metals": [], "oms": False, "n_atoms": None, "parse_ok": True,
             "science": _science_ready(f"{ZEOLITE_PREFIX}{c}")} for c in hits]


def search(q: str, limit: int = 30) -> list[dict]:
    """Pool structures whose name contains q (prefix matches first), plus IZA zeolite references."""
    idx = pool_index()
    q = (q or "").strip().lower()
    if not q:
        return []
    zeo = zeolite_hits(q)
    hits = [n for n in idx if q in n.lower()]
    hits.sort(key=lambda n: (not n.lower().startswith(q), len(n), n))
    pool = [{**idx[n], "science": _science_ready(n)} for n in hits[:limit]]
    # an explicit zeolite query lists zeolites first; a bare code ("MFI") puts them after the pool's prefix hits
    return (zeo + pool)[:limit] if zeo and ("zeol" in q or "iza" in q) else (pool[:max(0, limit - len(zeo[:5]))] + zeo[:5])


def _estimates(con: sqlite3.Connection, run_id: str) -> dict:
    from sieve.store import estimates_from_evaluations
    return estimates_from_evaluations(con, run_id)


def leaderboard(db: str, run_id: str) -> dict:
    """Every structure the run measured by GCMC (tier >= 1), with its best estimate and descriptors.
    run_id='*' merges all runs, keeping each structure's highest-tier estimate."""
    con = _con(db)
    runs = {r["run_id"]: r for r in list_runs(db)}
    ids = list(runs) if run_id == "*" else [run_id]
    merged: dict[str, dict] = {}
    for rid in ids:
        if rid not in runs:
            continue
        for s, e in _estimates(con, rid).items():
            row = {"structure": s, "uptake": round(e.uptake, 4), "err": round(e.err, 4), "tier": e.tier,
                   "n": e.n_evals, "run": rid, "arm": runs[rid]["arm"],
                   "t1": None, "t2": None}
            for t, vals in e.tiers.items():
                row[f"t{t}"] = round(sum(u for u, _ in vals) / len(vals), 4)
            prev = merged.get(s)
            if prev is None or (row["tier"], -row["err"]) > (prev["tier"], -prev["err"]):
                merged[s] = row
    idx = pool_index()
    rows = []
    for s, r in merged.items():
        d = idx.get(s, {})
        rows.append({**r, "PLD": d.get("PLD"), "LCD": d.get("LCD"), "VF": d.get("VF"),
                     "density": d.get("density"), "metals": d.get("metals", []), "oms": d.get("oms"),
                     "science": _science_ready(s), "props": _props_brief(s)})
    rows.sort(key=lambda r: r["uptake"], reverse=True)
    return {"run": dict(runs[run_id]) if run_id in runs else None, "rows": rows}


def runs_summary(db: str) -> list[dict]:
    """Per run: spend, how many structures each tier measured, best tier-1 and best tier-2 value."""
    con = _con(db)
    out = []
    for r in list_runs(db):
        rid = r["run_id"]
        spent = con.execute("SELECT COALESCE(SUM(cpu_s),0) FROM evaluations WHERE run_id=? AND "
                            "status!='running'", (rid,)).fetchone()[0]
        counts = {t: n for t, n in con.execute(
            "SELECT tier, COUNT(DISTINCT structure) FROM evaluations WHERE run_id=? AND status='ok' "
            "GROUP BY tier", (rid,))}
        est = _estimates(con, rid)
        best = lambda tier: max((e for e in est.values() if e.tier == tier),  # noqa: E731
                                key=lambda e: e.uptake, default=None)
        pack = lambda e: ({"structure": e.structure, "uptake": round(e.uptake, 4),  # noqa: E731
                           "err": round(e.err, 4)} if e else None)
        out.append({**r, "spent_cpu_s": round(spent, 1), "n_t1": counts.get(1, 0),
                    "n_t2": counts.get(2, 0), "best_t1": pack(best(1)), "best_t2": pack(best(2))})
    return out


def measurements(db: str, name: str) -> list[dict]:
    """Every GCMC measurement of one structure, across all runs."""
    con = _con(db)
    return [dict(r) for r in con.execute(
        "SELECT e.run_id, r.arm, e.tier, ROUND(e.uptake_molkg, 4) AS uptake, ROUND(e.uptake_err, 4) AS err, "
        "ROUND(e.cpu_s, 1) AS cpu_s, e.mode, e.finished_at FROM evaluations e JOIN runs r USING (run_id) "
        "WHERE e.structure=? AND e.tier>0 AND e.status='ok' ORDER BY e.tier DESC, e.finished_at", (name,))]


def events_after(db: str, run_id: str, after: int, T: float, limit: int = 500) -> list[dict]:
    con = _con(db)
    if not _has(con, "events"):
        return []
    rows = con.execute("SELECT event_id, ts, agent_id, parent_id, type, payload_json FROM events "
                       "WHERE run_id=? AND event_id>? AND ts<=? ORDER BY event_id LIMIT ?",
                       (run_id, after, T, limit)).fetchall()
    return [{"id": r["event_id"], "ts": r["ts"], "agent": r["agent_id"], "parent": r["parent_id"],
             "type": r["type"], "p": json.loads(r["payload_json"])} for r in rows]


def create_app(db: str, replay_run: str | None = None) -> FastAPI:
    app = FastAPI(title="SIEVE")

    def _prewarm() -> None:  # pymatgen import + pool table take seconds; not on the first request
        try:
            pool_points()
            import pymatgen.core  # noqa: F401
        except Exception:
            pass

    import threading
    threading.Thread(target=_prewarm, daemon=True).start()
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def no_cache(request, call_next):  # a demo machine must never run a stale app.js
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp
    sci_started: dict[str, float] = {}  # name -> when this server first saw it computing
    replay = {"run": replay_run, "t0": None, "ts0": None}  # the clock starts with the first viewer

    def clock(run_id: str | None) -> float:
        if replay["run"] and run_id == replay["run"]:
            if replay["ts0"] is None:
                r = [x for x in list_runs(db) if x["run_id"] == run_id]
                replay["ts0"] = r[0]["created_at"] if r else time.time()
            if replay["t0"] is None:
                replay["t0"] = time.time()
            return replay["ts0"] + (time.time() - replay["t0"]) * REPLAY_SPEED
        return time.time()

    def page(fname: str):
        from fastapi.responses import HTMLResponse

        import re

        html = (STATIC / fname).read_text()

        def ver(m):  # version every local asset by mtime so a browser never runs a stale one
            f = STATIC / m.group(1)
            return f"/static/{m.group(1)}?v={int(f.stat().st_mtime)}" if f.exists() else m.group(0)
        html = re.sub(r"/static/([\w.-]+\.(?:js|css))", ver, html)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    def index():
        return page("index.html")

    @app.get("/agents")
    def agents_page():
        return page("agents.html")

    @app.get("/compare")
    def compare_page():
        return page("compare.html")

    @app.get("/discoveries")
    def discoveries_page():
        return page("discoveries.html")

    @app.get("/guide")
    def guide_page():
        return page("guide.html")

    @app.get("/api/search")
    def api_search(q: str = "", limit: int = 30):
        return search(q, min(limit, 100))

    @app.get("/api/leaderboard")
    def api_leaderboard(run: str):
        return leaderboard(db, run)

    @app.get("/api/runs/summary")
    def api_runs_summary():
        return runs_summary(db)

    @app.get("/api/measurements")
    def api_measurements(name: str):
        return {"name": name, "pool": pool_index().get(name), "rows": measurements(db, name)}

    @app.get("/api/download/{kind}")
    def api_download(kind: str, name: str):
        """Files a scientist can take away: the simulated CIF, the energy cube, the isotherm."""
        from fastapi.responses import PlainTextResponse, Response
        from sieve import science

        stem = name.replace("/", "_")
        att = lambda fn: {"Content-Disposition": f'attachment; filename="{fn}"'}  # noqa: E731
        if kind == "cif":
            p = cif_payload(name)
            if p.get("error"):
                return Response(p["error"], status_code=404)
            return PlainTextResponse(p["cif"], headers=att(f"{stem}.cif"))
        if kind == "cube":
            p = science.cube_path(name)
            if not p.exists():
                return Response("no energy grid computed yet", status_code=404)
            return FileResponse(p, media_type="text/plain", headers=att(f"{stem}-co2-probe-energy.cube"))
        d = science.cached(name)
        if d is None:
            return Response("science data not computed yet", status_code=404)
        if kind == "isotherm.csv":
            rows = ["pressure_bar,uptake_molkg,uptake_err_molkg,temperature_K,forcefield"]
            rows += [f"{q['p_bar']},{'' if q['uptake'] is None else q['uptake']},"
                     f"{'' if q['err'] is None else q['err']},298,SieveUFF (UFF LJ + TraPPE CO2; no framework charges)"
                     for q in d.get("isotherm", [])]
            return PlainTextResponse("\n".join(rows) + "\n", media_type="text/csv",
                                     headers=att(f"{stem}-isotherm-298K.csv"))
        if kind == "json":
            return Response(json.dumps(d, indent=1), media_type="application/json",
                            headers=att(f"{stem}-science.json"))
        if kind in ("props.csv", "props.json"):
            pr = d.get("props")
            if not pr:
                return Response("capture properties not computed yet", status_code=404)
            if kind == "props.csv":
                return PlainTextResponse(science.props_csv(pr), media_type="text/csv",
                                         headers=att(f"{stem}-capture-props.csv"))
            return Response(json.dumps(pr, indent=1), media_type="application/json",
                            headers=att(f"{stem}-capture-props.json"))
        return Response("unknown download", status_code=404)

    @app.get("/api/discoveries")
    def api_discoveries(run: str):
        return discoveries(db, run)

    @app.get("/api/compare")
    def api_compare():  # computed live, so arms appear as their seeds finish
        from sieve.arena_eval import REPLAY_DB, report
        if not Path(REPLAY_DB).exists():
            return {"arms": {}}
        return report(Path(REPLAY_DB), [0.127, 0.25, 0.5, 1.0])

    @app.get("/api/findings")
    def api_findings():  # leads vs referee and disorder, same-model table, literature (Compare page)
        from sieve.dashboard.findings import findings
        return findings(db)

    @app.get("/api/config")
    def config():
        runs = list_runs(db)
        default = replay["run"] or (runs[0]["run_id"] if runs else None)
        return {"db": db, "replay": replay["run"], "speed": REPLAY_SPEED if replay["run"] else 1,
                "default_run": default, "runs": runs}

    @app.get("/api/pool")
    def api_pool():
        return pool_points()

    @app.get("/api/structure")
    def api_structure(name: str):
        return structure_payload(name)

    @app.get("/api/events")
    def api_events(run: str, limit: int = 300):
        T = clock(run)
        evs = events_after(db, run, 0, T, 100000)
        return evs[-limit:]

    @app.get("/api/viz")
    def api_viz(name: str):
        from sieve import viz
        out = viz.get(name)
        if out.get("status") == "ready":
            out["cif"] = cif_payload(name).get("cif")
        return out

    @app.get("/science")
    def science_page():
        return page("science.html")

    @app.get("/api/science")
    def api_science(name: str):
        from sieve import science
        out = science.get(name)
        if out.get("status") == "computing":
            out["since"] = sci_started.setdefault(name, time.time())
        if out.get("status") == "ready":
            cp = cif_payload(name)
            out["cif"] = cp.get("cif")
            out["density"] = cp.get("density")
        from sieve.oracle import zeolite_code
        if zeolite_code(name):
            out["zeolite"] = zeolite_code(name)
        return out

    @app.get("/api/science/cube")
    def api_science_cube(name: str):
        from fastapi.responses import PlainTextResponse
        from sieve import science
        p = science.cube_path(name)
        return PlainTextResponse(p.read_text() if p.exists() else "", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/review")
    def api_review(name: str):
        from sieve.agents import reviewer
        return reviewer.get(name)

    @app.get("/api/validation/chain")
    def api_validation_chain(run: str | None = None):
        from sieve.dashboard import validation
        return validation.chain(db, run)

    @app.get("/api/validation/reviews")
    def api_validation_reviews():
        from sieve.dashboard import validation
        return validation.reviews()

    @app.get("/api/validation/review")
    def api_validation_review(name: str):  # read-only: never starts a review (unlike /api/review)
        from sieve.dashboard import validation
        return validation.review(name)

    @app.get("/api/science/list")
    def api_science_list():
        from sieve import science
        typical = (Path(__file__).resolve().parents[3] / "data" / "science-typical.txt")
        items = [{"name": "ZADREE_clean", "role": "best confirmed on the full pool"},
                 {"name": "Cu-BTC", "role": "reference: HKUST-1"},
                 {"name": "MgMOF-74-VASP", "role": "reference: Mg-MOF-74"}]
        if typical.exists():
            items.append({"name": typical.read_text().strip(), "role": "a typical pool structure (median)"})
        from sieve.oracle import ZEOLITE_PREFIX, zeolite_code, zeolite_codes
        codes = set(zeolite_codes())
        computed = sorted(c for c in codes if (science.SCI_DIR / f"{ZEOLITE_PREFIX}{c}.json").exists())
        for c in [c for c in ZEOLITE_SET if c in codes] + [c for c in computed if c not in ZEOLITE_SET]:
            items.append({"name": f"{ZEOLITE_PREFIX}{c}", "role": "zeolite reference", "group": "zeolite", "zeolite": c,
                          "note": "pure-silica (no cations)"})
        seen = {i["name"] for i in items}
        if science.SCI_DIR.exists():  # anything a scientist asked for earlier, newest first
            extra = sorted((f for f in science.SCI_DIR.glob("*.json") if not f.name.startswith("review-") and not f.name.endswith(".props.json")),
                           key=lambda f: f.stat().st_mtime, reverse=True)
            for f in extra:
                try:
                    n = json.loads(f.read_text()).get("name") or f.stem
                except ValueError:
                    continue
                if n not in seen and not zeolite_code(n):
                    seen.add(n)
                    items.append({"name": n, "role": "computed on request"})
        return [{**i, "ready": (science.SCI_DIR / f"{i['name']}.json").exists()} for i in items]

    @app.get("/api/cif")
    def api_cif(name: str):
        return cif_payload(name)

    @app.get("/api/frame")
    def api_frame(run: str | None = None):
        return frame(db, run, clock(run))

    @app.get("/api/stream")
    async def stream(run: str | None = Query(None), backlog: int = 300):
        async def gen():
            last = 0
            if run:
                T = clock(run)
                try:
                    con = _con(db)
                    if _has(con, "events"):  # start `backlog` events back
                        row = con.execute("SELECT event_id FROM events WHERE run_id=? AND ts<=? "
                                          "ORDER BY event_id DESC LIMIT 1 OFFSET ?",
                                          (run, T, backlog)).fetchone()
                        last = row[0] if row else 0
                except sqlite3.OperationalError:
                    pass
            while True:
                T = clock(run)
                try:
                    f = await asyncio.to_thread(frame, db, run, T)
                    evs = await asyncio.to_thread(events_after, db, run, last, T) if run else []
                except sqlite3.OperationalError as e:  # DB missing or mid-checkpoint
                    f, evs = {"T": T, "runs": [], "run": None, "error": str(e)}, []
                if evs:
                    last = evs[-1]["id"]
                    yield f"event: events\ndata: {json.dumps(evs)}\n\n"
                yield f"event: frame\ndata: {json.dumps(f, default=str)}\n\n"
                await asyncio.sleep(FRAME_S)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    return app


def serve(db: str, port: int = 8770, replay_run: str | None = None) -> None:
    import uvicorn

    uvicorn.run(create_app(db, replay_run), host="127.0.0.1", port=port, log_level="warning")

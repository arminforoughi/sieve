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
    for r in q("SELECT agent_id, ts, type, payload_json FROM events WHERE run_id=? AND ts<=? AND "
               "type IN ('worker_start','task_start','task_done','timeout','task_error',"
               "'budget_refused','worker_dead','worker_restart') ORDER BY event_id", T):
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
    out["agent_rows"] = sorted(rows.values(), key=_agent_order)
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
        row = con.execute("SELECT run_id FROM runs WHERE arm=? AND (run_id=? OR status='finished') "
                          "ORDER BY (run_id=?) DESC, created_at DESC LIMIT 1",
                          (arm, run_id, run_id)).fetchone()
        if row:
            curves[arm] = {"run_id": row[0], "points": curve(con, row[0],
                                                             T if row[0] == run_id else 1e18)}
    out["curves"] = curves
    out["reference"] = None  # held-out best: revealed only when a referee/labels result exists
    if t_end and _has(con, "reference"):
        ref = con.execute("SELECT value, source FROM reference ORDER BY rowid DESC LIMIT 1"
                          ).fetchone()
        out["reference"] = dict(ref) if ref else None
    return out


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

    from sieve.oracle import PROJECT_ROOT, bundled_cif_dir, raspa_alias

    path = PROJECT_ROOT / "data" / "prepared" / f"{raspa_alias(name)}.cif"
    if not path.exists():
        for cand in (PROJECT_ROOT / "data" / "cifs" / f"{name}.cif",
                     bundled_cif_dir() / f"{name}.cif"):
            if cand.exists():
                path = cand
                break
        else:
            return {"name": name, "error": "structure file not found"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = Structure.from_file(str(path))
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

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

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

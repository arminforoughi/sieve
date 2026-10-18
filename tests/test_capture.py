"""Tier 3 (capture check), the finalist checks (disorder re-measure, capture) and run options."""

import asyncio
import json
import math
import time
import uuid
from types import SimpleNamespace

import pytest

from sieve import capture, pool
from sieve.agents import checks
from sieve.oracle import CostModel, Oracle
from sieve.store import Store, estimates_from_evaluations, resume


# ----------------------------------------------------------------------------------- arithmetic

def test_aps_arithmetic():
    c = capture.aps(n_co2=2.0, e_co2=0.1, n_n2=0.5, e_n2=0.05, n_des=0.4, e_des=0.02)
    S = (2.0 / 0.5) / (0.15 / 0.85)
    assert c["S"] == pytest.approx(S)
    assert c["S_err"] == pytest.approx(S * math.hypot(0.1 / 2.0, 0.05 / 0.5))
    assert c["dn"] == pytest.approx(1.6) and c["dn_err"] == pytest.approx(math.hypot(0.1, 0.02))
    assert c["aps"] == pytest.approx(S * 1.6)
    assert c["aps_err"] == pytest.approx(S * 1.6 * math.hypot(c["S_err"] / S, c["dn_err"] / 1.6))
    # no N2 adsorbed: selectivity undefined, no APS (never a division by zero)
    assert capture.aps(1.0, 0.1, 0.0, 0.0, 0.1, 0.01)["aps"] is None
    # desorption above the mixture loading: a negative working capacity, reported as such
    assert capture.aps(0.2, 0.01, 0.5, 0.05, 0.3, 0.01)["aps"] < 0


def test_objective_default_and_unknown():
    assert capture.objective({}) == "uptake" and capture.objective(None) == "uptake"
    assert capture.objective({"objective": "capture"}) == "capture"
    assert capture.objective({"objective": "nonsense"}) == "uptake"


# ------------------------------------------------------------------------------- ledger, store

def _row(con, run, s, tier, status="ok", u=None, e=None, cpu=10.0, result=None, hb=None):
    now = time.time()
    con.execute("INSERT INTO evaluations (eval_id, run_id, structure, tier, mode, status, est_cpu_s, cpu_s, "
                "uptake_molkg, uptake_err, result_json, started_at, heartbeat_cpu_s, finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, run, s, tier, "capture" if tier == 3 else "direct", status, cpu, cpu, u, e,
                 json.dumps(result or {}), now, hb, None if status == "running" else now))


def _cap(aps, err=0.1, S=10.0, dn=1.0):
    return {"capture": {"aps": aps, "aps_err": err, "S": S, "S_err": 1.0, "dn": dn, "dn_err": 0.05,
                        "n_mix_co2": 1.2, "n_mix_n2": 0.1, "n_des": 0.2}}


def test_tier3_ledger_and_estimates(tmp_path):
    db = tmp_path / "t.db"
    st = Store(db)
    run = st.create_run("sieve", 1000.0, {"objective": "capture"}, run_id="r")
    _row(st.con, run, "A", 1, u=2.0, e=0.2, cpu=20)
    _row(st.con, run, "A", 2, u=1.8, e=0.05, cpu=60)
    _row(st.con, run, "A", 3, cpu=40, result=_cap(5.0, 0.5))
    _row(st.con, run, "A", 3, cpu=40, result=_cap(6.0, 0.5))
    _row(st.con, run, "B", 3, status="timeout", cpu=30)
    _row(st.con, run, "C", 3, status="running", cpu=0, hb=12.0)
    est = estimates_from_evaluations(st.con, run)
    assert set(est) == {"A"} and est["A"].tier == 2 and est["A"].uptake == pytest.approx(1.8)  # tier 3 ignored
    cap = capture.estimates(st.con, run)
    assert set(cap) == {"A"} and cap["A"]["aps"] == pytest.approx(5.5) and cap["A"]["n"] == 2
    o = Oracle(db, run, budget_cpu_s=1000.0, labels_path=None)
    assert o.spent() == pytest.approx(20 + 60 + 40 + 40 + 30 + 0)  # every tier is in the ledger
    assert o.known_uptake("A") == pytest.approx(1.8)               # never a tier-3 NULL
    m = o.fit_cost_model(run_only=True)                            # tier-3 rows must not break the fit
    assert isinstance(m, CostModel) and m.mix_k > 0
    s = resume(db, run)                                            # in-flight tier 3: lost, charged
    assert s.reconciled["lost_evaluations"] == 1 and s.reconciled["lost_cpu_s"] == pytest.approx(12.0)
    assert o.spent() == pytest.approx(190 + 12)


def test_capture_plan_scales_with_cell_and_loading(tmp_path):
    from sieve.oracle import Descriptors
    o = Oracle(tmp_path / "p.db", "p", labels_path=None)
    small = Descriptors("x", True, alias="x", n_atoms=100, n_atoms_sim=800, unit_cells=(2, 2, 2), density=1.0,
                        volume=1000.0)
    big = Descriptors("y", True, alias="y", n_atoms=400, n_atoms_sim=3200, unit_cells=(2, 2, 2), density=1.0,
                      volume=4000.0)
    a, b = capture.plan(o, small, CostModel(), 1.0), capture.plan(o, big, CostModel(), 1.0)
    assert 0 < a.est_cpu_s < b.est_cpu_s and a.des_mode == "direct"
    assert capture.plan(o, big, CostModel(), 8.0).est_mix_s > b.est_mix_s   # more CO2, more moves
    assert capture.cpu_limit(o, a) <= capture.MAX_TIMEOUT_S


# ------------------------------------------------------------------------- finalist rules

def _ctx(tmp_path, objective="capture", disorder_check=True, flags=None, monkeypatch=None):
    st = Store(tmp_path / "c.db")
    run = st.create_run("sieve", 1000.0, {"objective": objective}, run_id="c")
    monkeypatch.setattr(checks, "_scan_table", lambda: {k: {"ok": True, "disordered": v, "clash_atom_fraction": 0.1 if v else 0}
                                                        for k, v in (flags or {}).items()})
    checks._lazy.clear()
    world = SimpleNamespace(idx={k: i for i, k in enumerate(["A_clean", "B_clean", "D_clean", "E_clean"])})
    ctx = SimpleNamespace(st=st, run_id=run, objective=objective, disorder_check=disorder_check, mixed=False,
                          vetoed={}, required={}, world=world, round=1, critic_notes="", models={},
                          oracle=Oracle(tmp_path / "c.db", run, 1000.0, labels_path=None), last_phase=None)
    return ctx


def test_capture_rule_needs_tier3_and_ranks_by_aps(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, flags={"A_clean": False, "B_clean": False, "D_clean": False}, monkeypatch=monkeypatch)
    con = ctx.st.con
    _row(con, "c", "A_clean", 2, u=5.0, e=0.1)      # best uptake, no capture check
    _row(con, "c", "B_clean", 2, u=3.0, e=0.1)
    _row(con, "c", "B_clean", 3, result=_cap(4.0))
    _row(con, "c", "D_clean", 2, u=2.0, e=0.1)
    _row(con, "c", "D_clean", 3, result=_cap(9.0))  # lower uptake, higher APS
    _row(con, "c", "E_clean", 1, u=9.0, e=1.0)      # tier 1 only
    _row(con, "c", "E_clean", 3, result=_cap(20.0))  # (cannot happen via the allocator) still not allowed
    est, cap = estimates_from_evaluations(con, "c"), capture.estimates(con, "c")
    rec = checks.recommendable(ctx, est, cap)
    assert [r["structure"] for r in rec] == ["D_clean", "B_clean"]
    jobs = checks.pending(ctx, est, cap)
    assert {"structure": "A_clean", "tier": 3} in [{k: j[k] for k in ("structure", "tier")} for j in jobs]


def test_disorder_rule(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, objective="uptake", flags={"A_clean": True, "B_clean": True, "D_clean": False},
               monkeypatch=monkeypatch)
    con = ctx.st.con
    _row(con, "c", "A_clean", 2, u=5.0, e=0.1)
    _row(con, "c", "B_clean", 2, u=4.0, e=0.1)
    _row(con, "c", "D_clean", 2, u=3.0, e=0.1)
    est = estimates_from_evaluations(con, "c")
    # unchecked disordered leaders: not recommendable; the critic asks for a resolved re-measure
    assert [r["structure"] for r in checks.recommendable(ctx, est, {})] == ["D_clean"]
    jobs = checks.audit(ctx, est, {})
    assert {(j["structure"], j["tier"]) for j in jobs} == {("A_dres_clean", 2), ("B_dres_clean", 2)}
    # resolved values: A drops 40% (artifact, vetoed), B drops 5% (passes, resolved value is used)
    _row(con, "c", "A_dres_clean", 2, u=3.0, e=0.1)
    _row(con, "c", "B_dres_clean", 2, u=3.8, e=0.1)
    est = estimates_from_evaluations(con, "c")
    checks.audit(ctx, est, {})
    types = [r[0] for r in con.execute("SELECT type FROM events WHERE run_id='c'")]
    assert "disorder_artifact" in types and "A_clean" in ctx.vetoed and "B_clean" not in ctx.vetoed
    rec = checks.recommendable(ctx, est, {})
    assert rec[0]["structure"] == "B_clean" and rec[0]["uptake_t2"] == pytest.approx(3.8)
    assert rec[0]["uptake_t2_raw"] == pytest.approx(4.0) and "A_clean" not in [r["structure"] for r in rec]
    # the same rule holds when the model is unavailable: the orchestrator's fallback picks B
    from sieve.agents import llm, orchestrator

    async def down(*a, **k):
        raise llm.LLMError("rate limited")
    monkeypatch.setattr(llm, "chat", down)
    out = asyncio.run(orchestrator.recommend(ctx))
    assert out["structure"] == "B_clean" and out["uptake"] == pytest.approx(3.8) and out["disorder"] == "passed"
    # no disorder check configured: the old rule (best tier-2 estimate, raw)
    ctx.disorder_check = False
    assert checks.recommendable(ctx, est, {})[0]["structure"] == "A_clean"


def test_resolved_names_and_lookup(tmp_path, monkeypatch):
    assert pool.resolved_name("ZADREE_clean") == "ZADREE_dres_clean"
    assert pool.original_name("ZADREE_dres_clean") == "ZADREE_clean"
    assert pool.is_resolved("ZADREE_dres_clean") and not pool.is_resolved("ZADREE_clean")
    assert pool.cif_path("IZA-LTA").endswith("IZA-LTA.cif")
    from pymatgen.core import Lattice, Structure
    from sieve import disorder

    # a toy CIF with one doubled site (two O 0.4 A apart): the resolved file is written on first use
    cifs = tmp_path / "cifs"
    cifs.mkdir()
    Structure(Lattice.cubic(8.0), ["Cu", "O", "O"], [[0, 0, 0], [0.5, 0.5, 0.5], [0.55, 0.5, 0.5]]).to(
        filename=str(cifs / "TOY_clean.cif"), fmt="cif")
    monkeypatch.setattr(disorder, "RESOLVED_DIR", tmp_path / "resolved")
    p = pool.cif_path("TOY_dres_clean", cifs)
    assert p.endswith("TOY_dres_clean.cif") and (tmp_path / "resolved" / "TOY_dres_clean.cif").exists()
    assert disorder.scan(cifs / "TOY_clean.cif")["disordered"] and not disorder.scan(p)["disordered"]


# --------------------------------------------------------------------------------- run options

def test_run_options_round_trip(tmp_path):
    from sieve.agents.orchestrator import run_options

    st = Store(tmp_path / "o.db")
    cfg = {"objective": "capture", "disorder_check": True, "limit": 50, "pool": "mixed", "seed": 1}
    st.create_run("sieve-v5", 100.0, cfg, run_id="o")
    back = resume(tmp_path / "o.db", "o").config
    assert run_options(back) == {"objective": "capture", "pool_kind": "mixed", "limit": 50, "disorder_check": True}
    # a run from before these options existed resumes exactly as it ran
    old = {"seed": 0, "pool": "CoRE-2019-ASR, PLD >= 3.3 A", "cost_model": CostModel().__dict__}
    assert run_options(old) == {"objective": "uptake", "pool_kind": "mof", "limit": None, "disorder_check": False}
    # an old cost model dict (no mix_k) still loads
    d = dict(CostModel().__dict__)
    d.pop("mix_k")
    assert CostModel(**d).mix_k == CostModel().mix_k


def test_stratified_sample_keeps_zeolites():
    c = [pool.Candidate(f"M{i}", "") for i in range(1000)] + [pool.Candidate(f"IZA-{i}", "", pool.ZEOLITE)
                                                              for i in range(30)]
    s = pool.sample(c, 40, 0)
    assert len(s) == 40 and sum(x.family == pool.ZEOLITE for x in s) == 10
    mof_only = [x for x in c if x.family == pool.MOF]
    import random
    assert pool.sample(mof_only, 40, 3) == random.Random(3).sample(mof_only, 40)  # unchanged for MOF pools

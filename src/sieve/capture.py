"""Tier 3, the capture check: rank finalists by flue-gas capture performance, not 1-bar uptake.

Run-config "objective": "uptake" (default; every earlier run) | "capture". Under "capture" the
screen itself is unchanged (tiers 1/2 are pure-CO2 uptake at 1 bar, the cheap signal that finds
candidates), but only tier-2-confirmed structures get a tier-3 capture check, and the winner is
the tier-3-measured structure with the highest adsorbent performance score

    APS = S(CO2/N2) x dN_mix                                             [mol/kg]
    S      = (x_CO2 / x_N2) / (y_CO2 / y_N2)   binary GCMC, 15:85 CO2:N2, 1 bar total, 298 K
    dN_mix = N_CO2(mixture, 1 bar) - N_CO2(pure CO2, 0.01 bar, 298 K)    (vacuum-swing desorption)

APS as "selectivity x working capacity" follows Chung, Y. G. et al., "In silico discovery of
metal-organic frameworks for precombustion CO2 capture using a genetic algorithm", Sci. Adv. 2,
e1600909 (2016). Errors: RASPA block errors, relative errors in quadrature (as science.py).
The selectivity and dN_mix definitions are science.py's ("selectivity", "wc_mix"), so a tier-3 row
and the Science page agree for the same structure and cycles.

One tier-3 evaluation = two RASPA calls in one budgeted row (tier=3, mode='capture'):
  des   pure CO2 at 0.01 bar; on the run's cached CO2 grid if there is one, else direct
  mix   15:85 CO2/N2 at 1 bar, direct (force field SieveUFFMix = SieveUFF + TraPPE N2)
CAPTURE_CYCLES + CAPTURE_INIT each, the science page's capture settings. Cost model:
  des  as tier-1/2 moves (direct_k or grid_mc_k) with N(0.01 bar) ~ N_DES_RATIO x N(1 bar)
  mix  direct_a + mix_k x moves x atoms, moves = cycles x max(20, N_mix), N_mix ~ N_MIX_RATIO x N(1 bar)
       (five science runs: N_mix/N(1 bar) 0.27-0.78, mix_k 2.2-3.1e-6 per move x atom)
Heartbeats, the CPU limit, timeouts, the ledger and resume are the oracle's own (_reserve,
_run_child, _finish). uptake_molkg stays NULL on tier-3 rows (they measure no 1-bar uptake), so
every tier-1/2 ranking ignores them; the capture numbers live in result_json["capture"].
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sieve import forcefield
from sieve.oracle import (GRID_SPACING_A, MIN_STEPS_PER_CYCLE, TIMEOUT_FACTOR, TIMEOUT_FLOOR_S, BudgetExhausted, CostModel,
                          Descriptors, Evaluation, Oracle)

TIER = 3
OBJECTIVES = ("uptake", "capture")
CAPTURE_CYCLES, CAPTURE_INIT = 500, 200
FLUE_Y = {"CO2": 0.15, "N2": 0.85}
P_TOTAL_PA = 1.0e5
P_DES_PA = 1.0e3          # 0.01 bar
T_K = 298.0
N_MIX_RATIO = 0.4
N_DES_RATIO = 0.03
MAX_TIMEOUT_S = 2400.0
CAPTURE_K = 3             # finalists that get a capture check at the end (SIEVE and baselines alike)


def objective(config: dict | None) -> str:
    o = str((config or {}).get("objective") or "uptake").lower()
    return o if o in OBJECTIVES else "uptake"


# ------------------------------------------------------------------------------------ arithmetic

def _q(a: float, b: float) -> float:
    return math.sqrt(a * a + b * b)


def aps(n_co2: float, e_co2: float, n_n2: float, e_n2: float, n_des: float, e_des: float,
        y_co2: float = FLUE_Y["CO2"], y_n2: float = FLUE_Y["N2"]) -> dict:
    """S, dN_mix and APS = S x dN_mix with errors (relative errors in quadrature)."""
    out = {"n_mix_co2": n_co2, "n_mix_co2_err": e_co2, "n_mix_n2": n_n2, "n_mix_n2_err": e_n2,
           "n_des": n_des, "n_des_err": e_des}
    dn, dn_err = n_co2 - n_des, _q(e_co2, e_des)
    out.update(dn=dn, dn_err=dn_err)
    if not n_n2 or n_n2 <= 0 or not n_co2 or n_co2 <= 0:
        out.update(S=None, S_err=None, aps=None, aps_err=None)
        return out
    S = (n_co2 / n_n2) / (y_co2 / y_n2)
    S_err = S * _q(e_co2 / n_co2, e_n2 / n_n2)
    a = S * dn
    rel = _q(S_err / S, dn_err / dn) if dn else float("inf")
    out.update(S=S, S_err=S_err, aps=a, aps_err=abs(a) * rel if dn else abs(S) * dn_err)
    return out


# ---------------------------------------------------------------------------------------- plan

@dataclass
class CapturePlan:
    est_cpu_s: float
    est_mix_s: float
    est_des_s: float
    des_mode: str           # grid | direct


def plan(o: Oracle, desc: Descriptors, model: CostModel | None = None,
         uptake_hint: float | None = None) -> CapturePlan:
    m = model or CostModel()
    u = uptake_hint if uptake_hint is not None else o.known_uptake(desc.structure)
    n1 = m.molecules(desc, u if u is not None else m.uptake_prior)
    cyc = CAPTURE_CYCLES + CAPTURE_INIT
    mix = m.direct_a + getattr(m, "mix_k", 2.9e-6) * cyc * max(MIN_STEPS_PER_CYCLE, N_MIX_RATIO * n1) * desc.n_atoms_sim
    moves_des = cyc * max(MIN_STEPS_PER_CYCLE, N_DES_RATIO * n1)
    direct = m.direct_a + m.direct_k * moves_des * desc.n_atoms_sim
    grid = m.grid_mc_a + m.grid_mc_k * moves_des
    use_grid = o.env.has_grid(o.framework_name(desc)) and grid < direct
    des = grid if use_grid else direct
    return CapturePlan(mix + des, mix, des, "grid" if use_grid else "direct")


def cpu_limit(o: Oracle, p: CapturePlan) -> float:
    limit = min(MAX_TIMEOUT_S, max(TIMEOUT_FLOOR_S, TIMEOUT_FACTOR * p.est_cpu_s))
    return max(1.0, min(limit, o.remaining()))


def _ensure_mix_forcefield(o: Oracle) -> None:
    ff = o.env.share / "forcefield" / forcefield.MIX_NAME
    if (ff / "pseudo_atoms.def").exists():
        return
    tmp = Path(tempfile.mkdtemp(dir=ff.parent, prefix=".ff-"))
    forcefield.write(tmp, mixture=True)
    try:
        os.rename(tmp, ff)
    except OSError:  # a concurrent job won the race
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------------------- run

def run(o: Oracle, desc: Descriptors, timeout_s: float | None = None, worker_id: str | None = None,
        cost_model: CostModel | None = None, uptake_hint: float | None = None) -> Evaluation:
    """One tier-3 capture check (two RASPA calls, one budgeted row). Blocks; never raises for a
    failed simulation. Raises BudgetExhausted if the estimate does not fit."""
    if not desc.parse_ok:
        raise ValueError(f"{desc.structure}: unusable structure ({desc.error})")
    if getattr(o, "_truth", None) is not None:
        raise ValueError("the capture check has no replay model (arena truth is 1-bar uptake only)")
    model = cost_model or CostModel()
    if uptake_hint is None:
        uptake_hint = o.known_uptake(desc.structure)
    p = plan(o, desc, model, uptake_hint)
    if timeout_s is None:
        timeout_s = cpu_limit(o, p)
    if o.remaining() < p.est_cpu_s:
        raise BudgetExhausted(f"{desc.structure} tier 3: needs ~{p.est_cpu_s:.0f}s, {o.remaining():.0f}s left")
    eval_id = o._reserve(desc.structure, TIER, "capture", p.est_cpu_s, timeout_s, worker_id)
    _ensure_mix_forcefield(o)
    t0 = time.time()
    fw = o.framework_name(desc)
    base = {"alias": fw, "cif_path": desc.prepared_path, "elements": desc.elements,
            "unit_cells": list(desc.unit_cells), "grid_types": list(forcefield.GRID_TYPES),
            "spacing": GRID_SPACING_A, "temperature": T_K, "helium_void_fraction": 0.75,
            "raspa_root": str(o.env.root), "cycles": CAPTURE_CYCLES, "init_cycles": CAPTURE_INIT}
    blk: dict = {}
    if getattr(o, "block_pockets", False):  # opt-in pore blocking, same rule as tiers 1/2
        try:
            f, _ = o.block_file(desc)
            blk = {"block_file": str(f)} if f else {}
        except Exception:  # noqa: BLE001
            blk = {}
    cost = {"n_atoms_sim": desc.n_atoms_sim, "cycles": CAPTURE_CYCLES + CAPTURE_INIT}
    phases: list[dict] = []
    used = lambda: sum(x["cpu_s"] for x in phases)  # noqa: E731
    cells = math.prod(desc.unit_cells)
    cyc = CAPTURE_CYCLES + CAPTURE_INIT

    des_kind = "mc_grid" if p.des_mode == "grid" else "mc_direct"
    ph = o._run_child({**base, **blk, "kind": des_kind, "forcefield": forcefield.NAME, "molecule": "CO2",
                       "pressure": P_DES_PA}, timeout_s, eval_id, 0.0)
    if ph["status"] == "ok":
        ph["moves"] = cyc * max(MIN_STEPS_PER_CYCLE, ph["result"]["molecules_per_cell"] * cells)
    phases.append({**ph, **cost, "point": "des_0.01bar"})
    if ph["status"] == "ok":
        ph = o._run_child({**base, **blk, "kind": "mc_mixture", "forcefield": forcefield.MIX_NAME,
                           "molecule": "CO2", "pressure": P_TOTAL_PA,
                           "components": [{"molecule": g, "mol_fraction": y} for g, y in FLUE_Y.items()]},
                          max(1.0, timeout_s - used()), eval_id, used())
        if ph["status"] == "ok":
            n = sum(v["molecules_per_cell"] for v in ph["result"]["loadings"].values()) * cells
            ph["moves"] = cyc * max(MIN_STEPS_PER_CYCLE, n)
        phases.append({**ph, **cost, "point": "mix_15:85_1bar"})
    cpu, wall = used(), time.time() - t0
    last = phases[-1]
    meta = {"plan": asdict(p), "phases": phases, "unit_cells": list(desc.unit_cells),
            "n_atoms_sim": desc.n_atoms_sim, "uptake_hint": uptake_hint,
            "conditions": {"flue_y": FLUE_Y, "p_total_bar": P_TOTAL_PA / 1e5, "p_des_bar": P_DES_PA / 1e5,
                           "T_K": T_K, "cycles": CAPTURE_CYCLES, "init_cycles": CAPTURE_INIT}}
    status = last["status"]
    if status == "timeout":
        o.con.execute("INSERT INTO timeouts VALUES (?,?,?,?,?,?,?,?)",
                      (eval_id, o.run_id, desc.structure, TIER, last["kind"], timeout_s, desc.n_atoms_sim, time.time()))
    if status != "ok":
        o._finish(eval_id, status, cpu, wall, result=meta)
        return Evaluation(eval_id, desc.structure, TIER, "capture", status, None, None, cpu, wall, timeout_s,
                          p.est_cpu_s, meta)
    des = phases[0]["result"]
    lo = last["result"]["loadings"]
    meta["capture"] = aps(lo["CO2"]["molkg"], lo["CO2"]["err"], lo["N2"]["molkg"], lo["N2"]["err"],
                          des["uptake_molkg"], des["uptake_err"])
    o._finish(eval_id, "ok", cpu, wall, None, None, meta)
    return Evaluation(eval_id, desc.structure, TIER, "capture", "ok", None, None, cpu, wall, timeout_s,
                      p.est_cpu_s, meta)


# ----------------------------------------------------------------------------------- estimates

def estimates(con, run_id: str) -> dict[str, dict]:
    """structure -> capture result (inverse-variance mean of APS over its ok tier-3 rows; S and dN
    from the most precise row). Derived from the evaluations table alone, like every estimate."""
    import json

    per: dict[str, list[dict]] = {}
    for s, rj in con.execute("SELECT structure, result_json FROM evaluations WHERE run_id=? AND tier=3 "
                             "AND status='ok' ORDER BY finished_at", (run_id,)):
        c = (json.loads(rj or "{}") or {}).get("capture")
        if c and c.get("aps") is not None:
            per.setdefault(s, []).append(c)
    out = {}
    for s, rows in per.items():
        w = [1.0 / max(r["aps_err"] or 1e-6, 1e-6) ** 2 for r in rows]
        mean = sum(wi * r["aps"] for wi, r in zip(w, rows)) / sum(w)
        best = max(zip(w, rows), key=lambda t: t[0])[1]
        out[s] = {**best, "aps": mean, "aps_err": (1.0 / sum(w)) ** 0.5, "n": len(rows)}
    return out


def brief(c: dict | None) -> dict | None:
    if not c:
        return None
    r = lambda x: None if x is None else round(float(x), 4)  # noqa: E731
    return {"aps": r(c.get("aps")), "aps_err": r(c.get("aps_err")), "S": r(c.get("S")), "S_err": r(c.get("S_err")),
            "dn": r(c.get("dn")), "dn_err": r(c.get("dn_err")), "n_mix_co2": r(c.get("n_mix_co2")),
            "n_mix_n2": r(c.get("n_mix_n2")), "n_des": r(c.get("n_des"))}

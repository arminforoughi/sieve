"""Science view: why a structure captures CO2, from the physics the simulation itself uses.

  energy field  RASPA's own van der Waals energy grid for a CO2 oxygen probe (O_co2, the strongest
                LJ site of TraPPE CO2) over the unit cell; written as a Gaussian cube file for 3D
                isosurfaces. The deepest wells are the binding pockets.
  pocket stats  the distribution of probe energies over the accessible pore volume
  isotherm      GCMC uptake at several pressures (298 K). 0.15 bar is the CO2 partial pressure of
                flue gas, so uptake there is what matters for post-combustion capture.

  capture props CO2/N2 selectivity from 15:85 flue-gas mixture GCMC, VSA / mixture / TSA working
                capacities, isosteric heat and Henry constants from Widom insertion
                (compute_props; cached separately in <alias>.props.json, merged by cached()).

All of this is visualization, run outside every budget, cached under data/science/.
Caveat: no framework partial charges in this model, so frameworks whose CO2 affinity is
electrostatic (open metal sites such as Mg-MOF-74) are under-rewarded relative to experiment.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import tempfile
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from sieve import forcefield
from sieve.oracle import PROJECT_ROOT, Oracle, bundled_cif_dir, describe, raspa_alias

SCI_DIR = PROJECT_ROOT / "data" / "science"
PRESSURES_BAR = (0.05, 0.15, 0.5, 1.0, 2.0, 5.0)
REFERENCES = {"Cu-BTC": "HKUST-1 (Cu-BTC)", "MgMOF-74-VASP": "Mg-MOF-74"}
K_TO_KJMOL = 0.0083145
BOHR = 0.529177
_busy: set[str] = set()
_lock = threading.Lock()


def _cif(name: str) -> Path:
    for p in (PROJECT_ROOT / "data" / "cifs" / f"{name}.cif", bundled_cif_dir() / f"{name}.cif"):
        if p.exists():
            return p
    raise FileNotFoundError(name)


def _oracle(name: str) -> Oracle:
    SCI_DIR.mkdir(parents=True, exist_ok=True)
    return Oracle(SCI_DIR / "science.db", f"sci-{name}", budget_cpu_s=1e9, labels_path=None)


def _job(desc, o: Oracle, **kw) -> dict:
    return {"alias": desc.alias, "cif_path": desc.prepared_path, "elements": desc.elements,
            "unit_cells": list(desc.unit_cells), "forcefield": forcefield.NAME,
            "grid_types": list(forcefield.GRID_TYPES), "spacing": 0.5, "molecule": "CO2",
            "temperature": 298.0, "pressure": 100000.0, "helium_void_fraction": 0.75,
            "raspa_root": str(o.env.root), **kw}


def read_grid(path: Path) -> dict:
    """RASPA VDW grid: 128-byte header, then 8 float32 blocks of (nx+1)(ny+1)(nz+1); block 0 = energy (K)."""
    b = path.read_bytes()
    spacing = struct.unpack("d", b[0:8])[0]
    n = np.array(struct.unpack("3i", b[8:20]))
    size = np.array(struct.unpack("3d", b[20:44]))
    shift = np.array(struct.unpack("3d", b[44:68]))
    m = int(np.prod(n + 1))
    e = np.frombuffer(b[128:128 + 4 * m], dtype=np.float32).reshape(tuple(n + 1))
    return {"spacing": spacing, "n": n, "size": size, "shift": shift, "energy_K": e}


def energy_field(name: str) -> dict:
    """Build RASPA's grid for the structure (0.5 A), return O_co2 probe energies in kJ/mol."""
    desc = describe(_cif(name), PROJECT_ROOT / "data" / "prepared")
    o = _oracle(name)
    private = o.env.private_root()
    try:
        p = o._run_child(_job(desc, o, kind="makegrid", alias=desc.alias, raspa_root=str(private)), 1200, "sci", 0.0)
        if p["status"] != "ok":
            raise RuntimeError(f"grid build {p['status']}")
        f = next(private.glob(f"share/raspa/grids/{forcefield.NAME}/*/*/*_O_co2_shifted.grid"))
        g = read_grid(f)
    finally:
        shutil.rmtree(private, ignore_errors=True)
    e = np.clip(g["energy_K"].astype(np.float64) * K_TO_KJMOL, -60.0, 20.0)
    return {**g, "kjmol": e, "desc": desc, "cpu_s": p["cpu_s"]}


def write_cube(path: Path, f: dict) -> None:
    """Gaussian cube (Bohr units) of the probe energy, axis-aligned box as RASPA tabulates it."""
    e, n, size, shift = f["kjmol"], f["n"], f["size"], f["shift"]
    step = 2 if np.prod(n) > 2.5e5 else 1  # keep the file browser-sized
    e = e[::step, ::step, ::step]
    d = size / n * step
    lines = ["SIEVE CO2 oxygen-probe van der Waals energy (kJ/mol)", "RASPA grid, SieveUFF + TraPPE",
             f"{1:5d} {shift[0] / BOHR:12.6f} {shift[1] / BOHR:12.6f} {shift[2] / BOHR:12.6f}"]
    for ax in range(3):
        v = [0.0, 0.0, 0.0]
        v[ax] = d[ax] / BOHR
        lines.append(f"{e.shape[ax]:5d} {v[0]:12.6f} {v[1]:12.6f} {v[2]:12.6f}")
    lines.append(f"{1:5d} {0.0:12.6f} {shift[0] / BOHR:12.6f} {shift[1] / BOHR:12.6f} {shift[2] / BOHR:12.6f}")
    flat = e.reshape(-1)
    body = "\n".join(" ".join(f"{x:.3f}" for x in flat[i:i + 6]) for i in range(0, len(flat), 6))
    path.write_text("\n".join(lines) + "\n" + body + "\n")


def pocket_stats(f: dict) -> dict:
    e = f["kjmol"].reshape(-1)
    acc = e[e < 0]  # accessible: attractive region (overlap is strongly positive)
    if not len(acc):
        return {}
    hist, edges = np.histogram(acc, bins=24, range=(-30, 0))
    return {"accessible_fraction": round(float(len(acc) / len(e)), 3),
            "min_kjmol": round(float(acc.min()), 2), "p05_kjmol": round(float(np.percentile(acc, 5)), 2),
            "median_kjmol": round(float(np.median(acc)), 2),
            "strong_fraction": round(float(np.mean(acc < -10.0)), 3),
            "hist": {"counts": hist.tolist(), "edges": [round(x, 2) for x in edges.tolist()]}}


def isotherm(name: str) -> list[dict]:
    desc = describe(_cif(name), PROJECT_ROOT / "data" / "prepared")

    def at(pbar):
        o = _oracle(name)  # one connection per thread: sqlite objects are not shareable
        p = o._run_child(_job(desc, o, kind="mc_direct", pressure=pbar * 1e5, cycles=500, init_cycles=150),
                         900, "sci", 0.0)
        if p["status"] != "ok":
            return {"p_bar": pbar, "uptake": None, "err": None}
        r = p["result"]
        return {"p_bar": pbar, "uptake": round(r["uptake_molkg"], 4), "err": round(r["uptake_err"], 4)}

    with ThreadPoolExecutor(3) as ex:
        return list(ex.map(at, PRESSURES_BAR))


def compute(name: str) -> dict:
    SCI_DIR.mkdir(parents=True, exist_ok=True)
    alias = raspa_alias(name)
    f = energy_field(name)
    write_cube(SCI_DIR / f"{alias}.cube", f)
    out = {"name": name, "label": REFERENCES.get(name, name.replace("_clean", "")),
           "pockets": pocket_stats(f), "isotherm": isotherm(name),
           "metals": f["desc"].metals, "n_atoms": f["desc"].n_atoms,
           "note": "visualization runs, outside every budget; no framework charges in this model"}
    (SCI_DIR / f"{alias}.json").write_text(json.dumps(out))
    return out


def cached_props(name: str) -> dict | None:
    f = props_path(name)
    return json.loads(f.read_text()) if f.exists() else None


def cached(name: str) -> dict | None:
    """The finished science result (with capture props under "props" if computed), or None.
    Never starts a computation."""
    f = SCI_DIR / f"{raspa_alias(name)}.json"
    if not f.exists():
        return None
    d = json.loads(f.read_text())
    pr = cached_props(name)
    return {**d, "props": pr} if pr else d


def get(name: str) -> dict:
    alias = raspa_alias(name)
    f = SCI_DIR / f"{alias}.json"
    if f.exists():
        return {"status": "ready", **cached(name)}
    err = SCI_DIR / f"{alias}.err"
    with _lock:
        if name not in _busy and not err.exists():
            _busy.add(name)

            def work():
                try:
                    compute(name)
                except Exception as e:  # noqa: BLE001
                    err.write_text(f"{type(e).__name__}: {e}")
                finally:
                    _busy.discard(name)
            threading.Thread(target=work, daemon=True).start()
    if err.exists() and name not in _busy:
        return {"status": "error", "error": err.read_text()}
    return {"status": "computing"}



# ------------------------------------------------------------------------------ capture properties
# Flue gas and cycle conditions. Pressures in bar, temperatures in K, loadings absolute mol/kg.
FLUE_Y = {"CO2": 0.15, "N2": 0.85}
P_FLUE_BAR = 1.0
T_ADS = 298.0
P_ADS_BAR = 0.15          # CO2 partial pressure of flue gas
P_DES_VSA_BAR = 0.01      # vacuum-swing desorption
T_DES_TSA = 393.0         # temperature-swing desorption, at the adsorption partial pressure
R_KJMOL = 0.0083144626
PROPS_CYCLES, PROPS_INIT, WIDOM_CYCLES = 500, 200, 2000

DEFINITIONS = {
    "selectivity": "S = (x_CO2/x_N2)/(y_CO2/y_N2): x = absolute adsorbed loadings (mol/kg) from a binary "
                   "GCMC of a 15:85 CO2:N2 gas at 1 bar total, 298 K (identity-change + swap moves); "
                   "y = gas mole fractions 0.15/0.85. Error: relative errors of the two loadings in quadrature.",
    "selectivity_0": "S_0 = K_H(CO2)/K_H(N2), infinite-dilution selectivity from Widom insertion at 298 K.",
    "wc_vsa": "WC_VSA = N_CO2(0.15 bar, 298 K) - N_CO2(0.01 bar, 298 K), both pure-CO2 GCMC (the 0.15 bar point "
              "is the science isotherm's). Vacuum swing; mol/kg.",
    "wc_mix": "WC_mix = N_CO2 from the 15:85 CO2:N2 mixture at 1 bar, 298 K minus pure-CO2 N(0.01 bar, 298 K). "
              "Adsorption with N2 competing, vacuum desorption; mol/kg.",
    "wc_tsa": "WC_TSA = N_CO2(0.15 bar, 298 K) - N_CO2(0.15 bar, 393 K), pure-CO2 GCMC. Temperature swing at "
              "constant partial pressure; mol/kg.",
    "qst": "Q_st^0 = -(<U_gh>_1 - <U_h>_0 - RT) at 298 K: isosteric heat at infinite dilution from Widom insertion "
           "(rigid TraPPE CO2, so <U_g> = 0). Positive = exothermic adsorption; kJ/mol.",
    "qst_fluct": "Q_st = -dH, RASPA's GCMC fluctuation formula dH = (<UN>-<U><N>)/(<N^2>-<N>^2) - RT, at the "
                 "pure-CO2 0.01 bar point and for the CO2 component of the flue-gas mixture; kJ/mol.",
    "k_h": "Henry constant K_H from Widom insertion at 298 K, mol/(kg bar) (RASPA reports mol/kg/Pa).",
    "errors": "All +/- are RASPA block-average errors (5 blocks), as for every SIEVE loading, propagated in quadrature.",
}
CAVEAT = ("No framework partial charges in SieveUFF, so the CO2 quadrupole never sees the framework: selectivity "
          "and Q_st are underestimated, most for open-metal-site and polar MOFs (Mg-MOF-74, HKUST-1). Dry flue gas: "
          "no water competition, which in reality degrades open-metal-site MOFs most.")


def props_path(name: str) -> Path:
    return SCI_DIR / f"{raspa_alias(name)}.props.json"


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


def _q(a, b):
    return math.sqrt(a * a + b * b)


def selectivity(x_co2: float, e_co2: float, x_n2: float, e_n2: float,
                y_co2: float = FLUE_Y["CO2"], y_n2: float = FLUE_Y["N2"]) -> dict:
    """Adsorption selectivity (x_CO2/x_N2)/(y_CO2/y_N2) with relative errors in quadrature."""
    if not x_n2 or x_n2 <= 0:
        return {"value": None, "err": None}
    s = (x_co2 / x_n2) / (y_co2 / y_n2)
    rel = _q(e_co2 / x_co2 if x_co2 else 0.0, e_n2 / x_n2)
    return {"value": s, "err": abs(s) * rel}


def working_capacity(ads: float, e_ads: float, des: float, e_des: float) -> dict:
    return {"value": ads - des, "err": _q(e_ads, e_des), "ads": ads, "des": des}


def qst_widom(u_kjmol: float, u_err: float, T: float = T_ADS) -> dict:
    """Infinite-dilution isosteric heat (positive, kJ/mol) from Widom's <U_gh>_1-<U_h>_0."""
    return {"value": -(u_kjmol - R_KJMOL * T), "err": u_err}


def derive_props(iso: list[dict], pts: dict, pockets: dict | None = None) -> dict:
    """Pure arithmetic from the raw GCMC/Widom points (see DEFINITIONS)."""
    ads = next((q for q in iso if abs(q["p_bar"] - P_ADS_BAR) < 1e-9 and q["uptake"] is not None), None)
    out: dict = {}
    mix, vsa, tsa, wid = (pts.get(k) or {} for k in ("mixture", "vsa_des", "tsa_des", "widom"))
    if mix.get("loadings"):
        c, n = mix["loadings"]["CO2"], mix["loadings"]["N2"]
        out["selectivity"] = selectivity(c["molkg"], c["err"], n["molkg"], n["err"])
    w = wid.get("widom") or {}
    kc, kn = w.get("CO2") or {}, w.get("N2") or {}
    if kc.get("kh_molkgpa") and kn.get("kh_molkgpa"):
        s0 = kc["kh_molkgpa"] / kn["kh_molkgpa"]
        out["selectivity_0"] = {"value": s0, "err": s0 * _q(kc["kh_err"] / kc["kh_molkgpa"],
                                                             kn["kh_err"] / kn["kh_molkgpa"])}
    out["k_h"] = {g: {"value": d["kh_molkgpa"] * 1e5, "err": d["kh_err"] * 1e5}
                  for g, d in (("CO2", kc), ("N2", kn)) if d.get("kh_molkgpa") is not None}
    wc = {}
    if ads and vsa.get("uptake_molkg") is not None:
        wc["vsa"] = working_capacity(ads["uptake"], ads["err"], vsa["uptake_molkg"], vsa["uptake_err"])
    if mix.get("loadings") and vsa.get("uptake_molkg") is not None:
        c = mix["loadings"]["CO2"]
        wc["mixture"] = working_capacity(c["molkg"], c["err"], vsa["uptake_molkg"], vsa["uptake_err"])
    if ads and tsa.get("uptake_molkg") is not None:
        wc["tsa"] = working_capacity(ads["uptake"], ads["err"], tsa["uptake_molkg"], tsa["uptake_err"])
    out["wc"] = wc
    q = {}
    if kc.get("u_kjmol") is not None:
        q["widom"] = qst_widom(kc["u_kjmol"], kc["u_err"])
    if kn.get("u_kjmol") is not None:
        q["widom_n2"] = qst_widom(kn["u_kjmol"], kn["u_err"])
    h_low = (vsa.get("enthalpy") or {}).get("_total")
    if h_low:
        q["fluct_low"] = {"value": -h_low["kjmol"], "err": h_low["err"], "p_bar": P_DES_VSA_BAR}
    h_mix = (mix.get("enthalpy") or {}).get("CO2")
    if h_mix:
        q["fluct_mix"] = {"value": -h_mix["kjmol"], "err": h_mix["err"]}
    out["qst"] = q
    if pockets and pockets.get("min_kjmol") is not None:
        out["deepest_well_kjmol"] = pockets["min_kjmol"]  # one O_co2 site, not a whole molecule
    if mix.get("loadings"):
        out["mixture"] = {g: {"molkg": d["molkg"], "err": d["err"]} for g, d in mix["loadings"].items()}
    return out


def _props_jobs(desc, o: Oracle) -> dict[str, tuple[dict, float]]:
    """name -> (job, CPU limit s). Production cycles PROPS_CYCLES per GCMC point."""
    mc = dict(cycles=PROPS_CYCLES, init_cycles=PROPS_INIT)
    return {
        "mixture": (_job(desc, o, kind="mc_mixture", forcefield=forcefield.MIX_NAME, temperature=T_ADS,
                         pressure=P_FLUE_BAR * 1e5, **mc,
                         components=[{"molecule": g, "mol_fraction": y} for g, y in FLUE_Y.items()]), 7200),
        "vsa_des": (_job(desc, o, kind="mc_direct", temperature=T_ADS, pressure=P_DES_VSA_BAR * 1e5,
                         enthalpy=True, **mc), 2400),
        "tsa_des": (_job(desc, o, kind="mc_direct", temperature=T_DES_TSA, pressure=P_ADS_BAR * 1e5,
                         enthalpy=True, **mc), 2400),
        "widom": (_job(desc, o, kind="widom", forcefield=forcefield.MIX_NAME, temperature=T_ADS,
                       cycles=WIDOM_CYCLES, init_cycles=0,
                       components=[{"molecule": g} for g in FLUE_Y]), 2400),
    }


def _run_point(name: str, key: str) -> tuple[str, dict]:
    desc = describe(_cif(name), PROJECT_ROOT / "data" / "prepared")
    o = _oracle(name)
    _ensure_mix_forcefield(o)
    job, limit = _props_jobs(desc, o)[key]
    p = o._run_child(job, limit, "sci", 0.0)
    r = p.get("result") or {}
    r.pop("lj_check", None)
    return key, {"status": p["status"], "cpu_s": round(p["cpu_s"], 1), "wall_s": round(p["wall_s"], 1),
                 **({"error": p.get("stderr", p.get("killed_by", ""))[-400:]} if p["status"] != "ok" else {}), **r}


def compute_props(name: str, ex: ThreadPoolExecutor | None = None) -> dict:
    """Capture properties for one structure (needs the base science JSON for the 0.15 bar point).
    Points that already finished OK in an earlier props file are reused."""
    base = cached(name)
    if base is None:
        base = compute(name)
    old = (cached_props(name) or {}).get("points", {})
    keys = [k for k in ("mixture", "vsa_des", "tsa_des", "widom") if (old.get(k) or {}).get("status") != "ok"]
    own = ex is None
    ex = ex or ThreadPoolExecutor(3)
    try:
        pts = {**old, **dict(f.result() for f in [ex.submit(_run_point, name, k) for k in keys])}
    finally:
        if own:
            ex.shutdown()
    out = {"name": name, "label": base.get("label", name), "points": pts,
           **derive_props(base.get("isotherm", []), pts, base.get("pockets")),
           "conditions": {"flue_y": FLUE_Y, "p_total_bar": P_FLUE_BAR, "T_ads_K": T_ADS, "p_ads_bar": P_ADS_BAR,
                          "p_des_vsa_bar": P_DES_VSA_BAR, "T_des_tsa_K": T_DES_TSA,
                          "cycles": PROPS_CYCLES, "init_cycles": PROPS_INIT, "widom_cycles": WIDOM_CYCLES},
           "definition": DEFINITIONS, "caveat": CAVEAT,
           "forcefield": f"{forcefield.MIX_NAME}: UFF LJ framework, TraPPE CO2 + TraPPE N2, LB mixing, 12.8 A "
                         "shifted cutoff, no charges anywhere",
           "computed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    tmp = props_path(name).with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    os.replace(tmp, props_path(name))
    return out


def props_csv(p: dict) -> str:
    """One row per quantity: quantity,value,err,unit,definition."""
    rows = ["quantity,value,err,unit,definition"]

    def add(q, d, unit, key):
        if d and d.get("value") is not None:
            rows.append(f"{q},{d['value']:.6g},{d['err']:.3g},{unit},\"{DEFINITIONS.get(key, '')}\"")
    add("selectivity_CO2_N2_15_85_1bar_298K", p.get("selectivity"), "-", "selectivity")
    add("selectivity_infinite_dilution", p.get("selectivity_0"), "-", "selectivity_0")
    wc = p.get("wc", {})
    add("wc_vsa_0.15to0.01bar_298K", wc.get("vsa"), "mol/kg", "wc_vsa")
    add("wc_mixture_flue_to_0.01bar", wc.get("mixture"), "mol/kg", "wc_mix")
    add("wc_tsa_298to393K_0.15bar", wc.get("tsa"), "mol/kg", "wc_tsa")
    q = p.get("qst", {})
    add("qst_CO2_infinite_dilution_widom", q.get("widom"), "kJ/mol", "qst")
    add("qst_CO2_fluctuation_0.01bar", q.get("fluct_low"), "kJ/mol", "qst_fluct")
    add("qst_CO2_fluctuation_mixture", q.get("fluct_mix"), "kJ/mol", "qst_fluct")
    add("qst_N2_infinite_dilution_widom", q.get("widom_n2"), "kJ/mol", "qst")
    for g, d in (p.get("k_h") or {}).items():
        add(f"henry_{g}_298K", d, "mol/(kg bar)", "k_h")
    for g, d in (p.get("mixture") or {}).items():
        add(f"mixture_loading_{g}", {"value": d["molkg"], "err": d["err"]}, "mol/kg", "selectivity")
    return "\n".join(rows) + "\n"


def cube_path(name: str) -> Path:
    return SCI_DIR / f"{raspa_alias(name)}.cube"


def _main(argv: list[str]) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m sieve.science")
    ap.add_argument("--props", action="store_true", help="compute capture properties (selectivity, WC, Qst)")
    ap.add_argument("--jobs", type=int, default=3, help="concurrent RASPA jobs (props only)")
    ap.add_argument("names", nargs="+")
    a = ap.parse_args(argv)
    if not a.props:
        for n in a.names:
            d = compute(n)
            print(n, d["pockets"].get("strong_fraction"), [(x["p_bar"], x["uptake"]) for x in d["isotherm"]], flush=True)
        return
    for n in a.names:  # base science first (it uses its own 3-job pool)
        if cached(n) is None:
            print(n, "computing base science first", flush=True)
            compute(n)
    with ThreadPoolExecutor(max(1, min(a.jobs, 3))) as ex:  # one pool: at most --jobs RASPA runs overall
        futs = {n: threading.Thread(target=lambda n=n: _report(n, compute_props(n, ex))) for n in a.names}
        for t in futs.values():
            t.start()
        for t in futs.values():
            t.join()


def _report(n: str, p: dict) -> None:
    f = lambda d: "-" if not d or d.get("value") is None else f"{d['value']:.3g}+/-{d['err']:.2g}"  # noqa: E731
    print(n, "S", f(p.get("selectivity")), "WC_vsa", f(p["wc"].get("vsa")), "WC_mix", f(p["wc"].get("mixture")),
          "WC_tsa", f(p["wc"].get("tsa")), "Qst", f(p["qst"].get("widom")),
          "status", {k: v["status"] for k, v in p["points"].items()}, flush=True)


if __name__ == "__main__":
    import sys

    _main(sys.argv[1:])

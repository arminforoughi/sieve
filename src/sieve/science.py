"""Science view: why a structure captures CO2, from the physics the simulation itself uses.

  energy field  RASPA's own van der Waals energy grid for a CO2 oxygen probe (O_co2, the strongest
                LJ site of TraPPE CO2) over the unit cell; written as a Gaussian cube file for 3D
                isosurfaces. The deepest wells are the binding pockets.
  pocket stats  the distribution of probe energies over the accessible pore volume
  isotherm      GCMC uptake at several pressures (298 K). 0.15 bar is the CO2 partial pressure of
                flue gas, so uptake there is what matters for post-combustion capture.

All of this is visualization, run outside every budget, cached under data/science/.
Caveat: no framework partial charges in this model, so frameworks whose CO2 affinity is
electrostatic (open metal sites such as Mg-MOF-74) are under-rewarded relative to experiment.
"""

from __future__ import annotations

import json
import math
import shutil
import struct
import threading
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


def cached(name: str) -> dict | None:
    """The finished science result, or None. Never starts a computation."""
    f = SCI_DIR / f"{raspa_alias(name)}.json"
    return json.loads(f.read_text()) if f.exists() else None


def get(name: str) -> dict:
    alias = raspa_alias(name)
    f = SCI_DIR / f"{alias}.json"
    if f.exists():
        return {"status": "ready", **json.loads(f.read_text())}
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


def cube_path(name: str) -> Path:
    return SCI_DIR / f"{raspa_alias(name)}.cube"


if __name__ == "__main__":
    import sys

    for n in sys.argv[1:]:
        d = compute(n)
        print(n, d["pockets"].get("strong_fraction"), [(x["p_bar"], x["uptake"]) for x in d["isotherm"]], flush=True)

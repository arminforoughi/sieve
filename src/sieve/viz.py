"""Visualization data for the 3D tour: where CO2 actually sits in a structure.

A short GCMC run (outside every budget, cached per structure) with RASPA's movie output gives
snapshots of the adsorbed CO2. Molecules are wrapped into the unit cell (by their carbon, so a
molecule is never split). The largest cavity is the sampled point farthest from any framework
atom. Nothing here feeds back into a run.
"""

from __future__ import annotations

import json
import math
import threading
import warnings
from pathlib import Path

import numpy as np

from sieve import forcefield
from sieve.oracle import PROJECT_ROOT, Oracle, core_descriptors, describe

VIZ_DIR = PROJECT_ROOT / "data" / "viz"
_busy: set[str] = set()
_lock = threading.Lock()


def _frames(pdb: str, lattice) -> list[list[list[float]]]:
    frames, cur = [], []
    for line in pdb.splitlines():
        if line.startswith("MODEL"):
            cur = []
        elif line.startswith(("ATOM", "HETATM")):
            cur.append([float(line[30:38]), float(line[38:46]), float(line[46:54])])
        elif line.startswith("ENDMDL"):
            frames.append(cur)
    if cur and (not frames or frames[-1] is not cur):
        frames.append(cur)
    out = []
    for atoms in frames:
        mols = []
        for k in range(0, len(atoms) - 2, 3):  # O, C, O
            o1, c, o2 = (np.array(a) for a in atoms[k:k + 3])
            f = lattice.get_fractional_coords(c)
            if np.all((f >= 0) & (f < 1)):  # only molecules inside the displayed unit cell: no
                mols.append([[round(float(v), 2) for v in p] for p in (o1, c, o2)])  # 8x crowding
        out.append(mols)
    return out


def _cavity(s) -> dict:
    rng = np.random.default_rng(1)
    trial = rng.random((4000, 3))
    d = s.lattice.get_all_distances(trial, s.frac_coords).min(axis=1)
    i = int(np.argmax(d))
    c = s.lattice.get_cartesian_coords(trial[i])
    return {"center": [round(float(v), 2) for v in c], "radius": round(float(d[i]) - 1.2, 2)}  # minus ~vdW radius


def compute(name: str) -> dict:
    from pymatgen.core import Structure

    VIZ_DIR.mkdir(parents=True, exist_ok=True)
    desc = describe(PROJECT_ROOT / "data" / "cifs" / f"{name}.cif", PROJECT_ROOT / "data" / "prepared")
    if not desc.parse_ok:
        raise ValueError(desc.error)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = Structure.from_file(desc.prepared_path)
    o = Oracle(VIZ_DIR / "viz.db", f"viz-{name}", budget_cpu_s=1e9, labels_path=None)
    job = {"kind": "movie", "alias": desc.alias, "cif_path": desc.prepared_path, "elements": desc.elements,
           "unit_cells": list(desc.unit_cells), "forcefield": forcefield.NAME,
           "grid_types": list(forcefield.GRID_TYPES), "spacing": 0.3, "molecule": "CO2",
           "temperature": 298.0, "pressure": 100000.0, "helium_void_fraction": 0.75,
           "raspa_root": str(o.env.root), "cycles": 300, "init_cycles": 100, "movie_every": 15}
    p = o._run_child(job, 900, "viz", 0.0)
    if p["status"] != "ok":
        raise RuntimeError(f"movie run {p['status']}: {p.get('stderr', '')[-200:]}")
    pdb = next((t for f, t in p["result"]["movie"].items() if "component_CO2" in f), "")
    frames = _frames(pdb, s.lattice)[-20:]
    cells = math.prod(desc.unit_cells)
    out = {"name": name, "frames": frames, "cavity": _cavity(s),
           "molecules_per_cell": round(p["result"]["molecules_per_cell"], 2),
           "uptake_molkg": p["result"]["uptake_molkg"], "cells_simulated": cells,
           "metals": desc.metals, "core": core_descriptors().get(name, {}),
           "note": "short visualization run (300 cycles), outside every budget",
           "raw_pdb": pdb}
    (VIZ_DIR / f"{desc.alias}.json").write_text(json.dumps(out))
    return out


def get(name: str) -> dict:
    """Cached tour data, or {'status': 'computing'} while a background run makes it."""
    from sieve.oracle import raspa_alias

    f = VIZ_DIR / f"{raspa_alias(name)}.json"
    if f.exists():
        d = json.loads(f.read_text())
        d.pop("raw_pdb", None)
        return {"status": "ready", **d}
    with _lock:
        if name not in _busy:
            _busy.add(name)

            def work():
                try:
                    compute(name)
                except Exception as e:  # noqa: BLE001
                    (VIZ_DIR / f"{raspa_alias(name)}.err").write_text(str(e))
                finally:
                    _busy.discard(name)

            threading.Thread(target=work, daemon=True).start()
    err = VIZ_DIR / f"{raspa_alias(name)}.err"
    return {"status": "error", "error": err.read_text()} if err.exists() and name not in _busy else {"status": "computing"}

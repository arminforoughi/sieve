"""Geometric pore descriptors on a distance grid -- for structures CoRE has no Zeo++ row for.

Zeo++ (`network`) is not installed here, so the 250 IZA zeolites get their descriptors from this
module instead. Every quantity follows the Zeo++ definitions CoRE MOF 2019 reports, computed on
a periodic grid of the distance-to-nearest-atom-surface field

    f(x) = min_i ( |x - x_i| - r_i )          (r_i: CCDC van der Waals radius of atom i)

  LCD   largest included sphere: 2 * max f, refined off-grid by a local optimisation
  PLD   largest free sphere that can percolate through the periodic framework: the largest r
        for which {f >= r} contains a connected component that wraps the cell. Found by bisection
        on r; each test labels {f >= r} inside the cell (scipy.ndimage.label) and joins labels
        across the periodic faces with a union-find that carries lattice offsets, so a cycle
        with a nonzero net offset is a path from a pore to its own image (percolation).
  VF    He-like probe (r = 0 here, see `calibration`): the share of the cell where a probe
        centre can sit, i.e. f >= r_probe, counting only the percolating (accessible) volume.
  ASA   Monte Carlo probe-centre surface: points on each atom's sphere of radius r_i + r_probe,
        kept if no other expanded sphere contains them and the probe there is in an accessible
        (percolating) channel; m2/g with probe 1.86 A (N2), as in CoRE MOF 2019.
  density from the cell contents.

Validation against CoRE's own Zeo++ numbers on CoRE MOFs is in `validate()` and
tests/test_porosity.py; the measured agreement (and any bias) is recorded in CALIBRATION below.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

# CCDC van der Waals radii (A), the Zeo++ default table for the elements that occur in CoRE and
# the IZA frameworks; anything else falls back to pymatgen's van der Waals radius, then 2.0.
RADII = {
    "H": 1.09, "He": 1.40, "Li": 1.82, "B": 1.92, "C": 1.70, "N": 1.55, "O": 1.52, "F": 1.47,
    "Ne": 1.54, "Na": 2.27, "Mg": 1.73, "Al": 1.84, "Si": 2.10, "P": 1.80, "S": 1.80, "Cl": 1.75,
    "Ar": 1.88, "K": 2.75, "Ca": 2.31, "Ga": 1.87, "Ge": 2.11, "As": 1.85, "Se": 1.90, "Br": 1.85,
    "Kr": 2.02, "Rb": 3.03, "Sr": 2.49, "In": 1.93, "Sn": 2.17, "Sb": 2.06, "Te": 2.06, "I": 1.98,
    "Xe": 2.16, "Cs": 3.43, "Ba": 2.68, "Tl": 1.96, "Pb": 2.02, "Bi": 2.07, "Ni": 1.63, "Cu": 1.40,
    "Zn": 1.39, "Ag": 1.72, "Cd": 1.58, "Pd": 1.63, "Pt": 1.75, "Au": 1.66, "Hg": 1.55, "U": 1.86,
}
SPACING_A = 0.20            # grid spacing; coarsened for big cells so a cell has <= MAX_POINTS
MAX_POINTS = 3_000_000
PLD_SPACING_A = 0.12        # the PLD's own, finer grid
PLD_MAX_POINTS = 4_000_000
ASA_PROBE_A = 1.86          # N2, the CoRE MOF 2019 ASA probe
VF_PROBE_A = 0.0            # see CALIBRATION: matches CoRE's AV_VF best
ASA_SAMPLES = 400           # Monte Carlo points per atom sphere
ZEOLITE_TABLE = Path(__file__).resolve().parents[2] / "data" / "zeolites" / "descriptors.json"

# validate() on 20 CoRE MOFs (LCD 3.7-17.8 A, stratified by LCD; scratch run 2026-10-17), this
# module minus CoRE's Zeo++ value: mean / mean |.| / range.
#   LCD   +0.016 / 0.041 A / -0.05..+0.49 A  (one outlier, SEGBIR +0.49; the rest within 0.06 A)
#   PLD   -0.089 / 0.089 A / -0.18..-0.00 A  (always low: a grid path misses the window's saddle)
#   AV_VF -0.002 / 0.005   / -0.012..+0.014
#   ASA   -232 / 232 m2/g  (-29% mean; -6..-15% for open frameworks, much lower or 0 where the
#                          window is within ~0.2 A of the 3.72 A N2 probe diameter)
# The PLD bias is one-signed, so it is calibrated out (PLD_BIAS_A is added; the raw grid value is
# kept as PLD_grid). ASA is reported as computed: it is the least reliable descriptor here, a
# ranking feature only. LCD and VF need no correction.
PLD_BIAS_A = 0.089
CALIBRATION: dict = {"n_core_mofs": 20, "LCD_mean_diff_A": 0.016, "LCD_mae_A": 0.041, "PLD_mean_diff_A": -0.089,
                     "PLD_mae_A": 0.089, "PLD_bias_added_A": PLD_BIAS_A, "VF_mean_diff": -0.002, "VF_mae": 0.005,
                     "ASA_mean_rel_diff": -0.29, "ASA_mae_m2_g": 232}


def radius(el: str) -> float:
    """CCDC radius; 2.00 A for elements without one (the CCDC convention for most metals)."""
    return RADII.get(el, 2.0)


@dataclass
class PoreDescriptors:
    LCD: float
    PLD: float
    AV_VF: float
    ASA_m2_g: float
    density: float
    spacing_A: float
    n_points: int
    pld_spacing_A: float = 0.0
    PLD_grid: float = 0.0       # before the calibration offset
    method: str = "sieve.porosity grid (CCDC radii)"


# ------------------------------------------------------------------------------- distance field

class Field:
    """f(x) = distance to the nearest atom surface, for a periodic structure."""

    def __init__(self, frac: np.ndarray, radii: np.ndarray, lattice: np.ndarray, reach: float = 14.0):
        from scipy.spatial import cKDTree

        self.M = np.asarray(lattice, dtype=float)              # rows = a, b, c (A)
        self.frac = np.mod(frac, 1.0)
        self.radii = np.asarray(radii, dtype=float)
        vol = abs(np.linalg.det(self.M))
        widths = [vol / np.linalg.norm(np.cross(self.M[(i + 1) % 3], self.M[(i + 2) % 3])) for i in range(3)]
        n = [max(1, math.ceil(reach / w)) for w in widths]     # images needed to see `reach` A away
        shifts = np.array([(i, j, k) for i in range(-n[0], n[0] + 1) for j in range(-n[1], n[1] + 1)
                           for k in range(-n[2], n[2] + 1)], dtype=float)
        img = (self.frac[None, :, :] + shifts[:, None, :]).reshape(-1, 3)
        self.xyz = img @ self.M
        self.r = np.tile(self.radii, len(shifts))
        self.tree = cKDTree(self.xyz)
        self.k = int(min(len(self.xyz), 16))

    def at_cart(self, pts: np.ndarray, chunk: int = 200_000) -> np.ndarray:
        out = np.empty(len(pts))
        for s in range(0, len(pts), chunk):
            d, i = self.tree.query(pts[s:s + chunk], k=self.k)
            if self.k == 1:
                d, i = d[:, None], i[:, None]
            out[s:s + chunk] = (d - self.r[i]).min(axis=1)
        return out

    def at_frac(self, f: np.ndarray) -> np.ndarray:
        return self.at_cart(np.mod(f, 1.0) @ self.M)


def grid_shape(lattice: np.ndarray, spacing: float = SPACING_A, max_points: int = MAX_POINTS) -> tuple:
    lengths = np.linalg.norm(lattice, axis=1)
    h = spacing
    while True:
        n = tuple(int(max(4, math.ceil(L / h))) for L in lengths)
        if n[0] * n[1] * n[2] <= max_points:
            return n, h
        h *= 1.1


def field_grid(F: Field, n: tuple) -> np.ndarray:
    axes = [np.arange(k) / k for k in n]
    g = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
    return F.at_frac(g).reshape(n)


# ---------------------------------------------------------------------------------- percolation

def _components(mask: np.ndarray) -> tuple[np.ndarray, set[int]]:
    """Label {mask} periodically. Returns (labels, set of labels that percolate, i.e. connect to
    one of their own periodic images)."""
    from scipy import ndimage

    lab, nlab = ndimage.label(mask)  # 6-connectivity inside the cell (26 leaks between atoms)
    if nlab == 0:
        return lab, set()
    parent = list(range(nlab + 1))
    off = [np.zeros(3, dtype=int) for _ in range(nlab + 1)]
    wraps = [False] * (nlab + 1)

    def find(x):
        o = np.zeros(3, dtype=int)
        path = []
        while parent[x] != x:
            path.append(x)
            o = o + off[x]
            x = parent[x]
        root, acc = x, o
        for p in path:  # path compression with offsets
            nxt = acc - off[p]
            off[p] = acc.copy()
            parent[p] = root
            acc = nxt
        return root, o

    for axis in range(3):
        a = np.take(lab, -1, axis=axis)   # last layer
        b = np.take(lab, 0, axis=axis)    # first layer, one cell further along `axis`
        both = (a > 0) & (b > 0)
        if not both.any():
            continue
        pairs = np.unique(np.stack([a[both], b[both]], axis=1), axis=0)
        s = np.zeros(3, dtype=int)
        s[axis] = 1
        for la, lb in pairs:
            ra, oa = find(int(la))
            rb, ob = find(int(lb))
            if ra == rb:
                if np.any(oa + s - ob):
                    wraps[ra] = True
            else:
                parent[rb] = ra
                off[rb] = oa + s - ob
                wraps[ra] = wraps[ra] or wraps[rb]
    perc = set()
    for x in range(1, nlab + 1):
        r, _ = find(x)
        if wraps[r]:
            perc.add(x)
    return lab, perc


def percolates(mask: np.ndarray) -> bool:
    return bool(_components(mask)[1])


def accessible(mask: np.ndarray) -> np.ndarray:
    lab, perc = _components(mask)
    if not perc:
        return np.zeros_like(mask, dtype=bool)
    return np.isin(lab, list(perc))


# ---------------------------------------------------------------------------------- descriptors

def _lcd(F: Field, g: np.ndarray, n: tuple) -> float:
    from scipy.optimize import minimize

    flat = g.reshape(-1)
    best = float(flat.max())
    for idx in np.argsort(flat)[-6:]:
        x0 = np.array(np.unravel_index(idx, n), dtype=float) / np.array(n)
        r = minimize(lambda x: -F.at_frac(x[None, :])[0], x0, method="Nelder-Mead",
                     options={"xatol": 1e-4, "fatol": 1e-4, "maxiter": 200})
        best = max(best, -float(r.fun))
    return 2.0 * best


def _pld(g: np.ndarray, tol: float = 0.005) -> float:
    lo, hi = 0.0, float(g.max())
    if not percolates(g >= lo):
        return 0.0
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if percolates(g >= mid):
            lo = mid
        else:
            hi = mid
    return 2.0 * lo


def _asa(F: Field, g: np.ndarray, n: tuple, mass_g: float, probe: float, rng: np.random.Generator,
         samples: int = ASA_SAMPLES, surface_probe: float | None = None) -> float:
    """probe: accessibility threshold on the grid; surface_probe: the probe radius whose centre
    surface is measured (defaults to probe)."""
    acc = accessible(g >= probe)
    probe = probe if surface_probe is None else surface_probe
    if not acc.any():
        return 0.0
    v = rng.normal(size=(samples, 3))
    v /= np.linalg.norm(v, axis=1)[:, None]
    cart0 = F.frac @ F.M
    area = 0.0
    nn = np.array(n)
    inv = np.linalg.inv(F.M)
    for i in range(len(F.frac)):
        R = F.radii[i] + probe
        pts = cart0[i] + R * v
        # outside every other atom's expanded sphere (f >= probe up to MC noise)
        keep = F.at_cart(pts) >= probe - 1e-6
        if not keep.any():
            continue
        p = pts[keep]
        fr = np.mod(p @ inv, 1.0)
        # the probe sits on the boundary of {f >= probe}: accessible if any of the 8 surrounding
        # grid nodes is accessible
        base = np.floor(fr * nn).astype(int)
        ok = np.zeros(len(p), dtype=bool)
        for dx in (0, 1):
            for dy in (0, 1):
                for dz in (0, 1):
                    ix = (base + np.array([dx, dy, dz])) % nn
                    ok |= acc[ix[:, 0], ix[:, 1], ix[:, 2]]
        area += 4.0 * math.pi * R * R * ok.sum() / samples
    return area * 1e-20 / mass_g  # A^2 -> m^2, per gram of framework


def compute(structure, spacing: float = SPACING_A, vf_probe: float = VF_PROBE_A,
            asa_probe: float = ASA_PROBE_A, seed: int = 0, radii: dict | None = None) -> PoreDescriptors:
    """Descriptors of a pymatgen Structure (periodic). radii overrides RADII per element (the IZA
    database's own convention, for instance, is O = 1.35 A).

    A path through grid nodes passes a window slightly off its saddle point unless the grid
    happens to hit it, so a grid PLD is low by up to ~spacing (0.0-0.3 A at 0.2 A). The PLD is
    therefore computed on a finer grid (PLD_SPACING_A); the accessibility masks for VF and ASA
    (on the 0.2 A grid) are lowered by half the coarse-vs-fine difference, so a channel whose
    window admits the probe on the fine grid is counted as open on the coarse one too."""
    els = [site.specie.symbol for site in structure]
    rad = {**RADII, **(radii or {})}
    F = Field(structure.frac_coords, np.array([rad.get(e, 2.0) for e in els]), structure.lattice.matrix)
    n, h = grid_shape(F.M, spacing)
    g = field_grid(F, n)
    pld_c = _pld(g)
    nf, hf = grid_shape(F.M, PLD_SPACING_A, PLD_MAX_POINTS)
    pld = max(pld_c, _pld(field_grid(F, nf))) if hf < h * 0.95 else pld_c
    delta = (pld - pld_c) / 2.0
    mass_g = float(structure.density) * float(structure.volume) * 1e-24
    vf = float(accessible(g >= vf_probe - (delta if vf_probe > 0 else 0.0)).mean())
    return PoreDescriptors(
        LCD=round(_lcd(F, g, n), 4), PLD=round(pld + PLD_BIAS_A, 4) if pld > 0 else 0.0, AV_VF=round(vf, 4),
        ASA_m2_g=round(float(_asa(F, g, n, mass_g, asa_probe - delta, np.random.default_rng(seed),
                                  surface_probe=asa_probe)), 1),
        density=round(float(structure.density), 4), spacing_A=round(h, 3), n_points=int(np.prod(n)),
        pld_spacing_A=round(min(h, hf), 3), PLD_grid=round(pld, 4))


def compute_cif(path, **kw) -> PoreDescriptors:
    import warnings

    from sieve.oracle import _load_structure

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return compute(_load_structure(path), **kw)


# ------------------------------------------------------------------------------------ zeolites

def zeolite_row(code: str) -> dict:
    """CoRE-style descriptor row for one IZA framework (pure SiO2: no metals, no open metal sites)."""
    from sieve.oracle import ZEOLITE_PREFIX, zeolite_cif

    d = compute_cif(zeolite_cif(f"{ZEOLITE_PREFIX}{code}"))
    return {**asdict(d), "Has_OMS": "No", "Open_Metal_Sites": None, "All_Metals": None, "DISORDER": None,
            "family": "zeolite", "LFPD": None}


def load_zeolite_table() -> dict[str, dict]:
    """IZA-<CODE> -> descriptor row, from data/zeolites/descriptors.json (built by build_zeolites)."""
    if not ZEOLITE_TABLE.exists():
        return {}
    try:
        return json.loads(ZEOLITE_TABLE.read_text()).get("rows", {})
    except ValueError:
        return {}


def build_zeolites(jobs: int = 3, codes: list[str] | None = None) -> dict:
    """Compute every downloaded IZA framework (resumable: finished codes are kept)."""
    import time
    from concurrent.futures import ProcessPoolExecutor, as_completed

    from sieve.oracle import ZEOLITE_PREFIX, zeolite_codes

    rows = load_zeolite_table()
    todo = [c for c in (codes or zeolite_codes()) if f"{ZEOLITE_PREFIX}{c}" not in rows]
    errors = {}

    def save():
        tmp = ZEOLITE_TABLE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"method": __doc__.split("\n")[0], "calibration": CALIBRATION,
                                   "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "rows": rows,
                                   "errors": errors}, indent=0))
        tmp.replace(ZEOLITE_TABLE)

    with ProcessPoolExecutor(max(1, jobs)) as ex:
        futs = {ex.submit(zeolite_row, c): c for c in todo}
        for i, f in enumerate(as_completed(futs), 1):
            c = futs[f]
            try:
                rows[f"{ZEOLITE_PREFIX}{c}"] = f.result()
            except Exception as e:  # noqa: BLE001
                errors[c] = f"{type(e).__name__}: {e}"
            if i % 10 == 0 or i == len(futs):
                save()
                print(f"{i}/{len(futs)} zeolites", flush=True)
    save()
    return rows


# ---------------------------------------------------------------------------------- validation

def validate(names: list[str], cif_dir: Path | None = None) -> list[dict]:
    """This module vs CoRE MOF 2019's Zeo++ numbers on the given CoRE structures."""
    from sieve.oracle import core_descriptors
    from sieve.pool import CIF_DIR

    core = core_descriptors()
    out = []
    for nm in names:
        d = compute_cif(Path(cif_dir or CIF_DIR) / f"{nm}.cif")
        c = core.get(nm, {})
        out.append({"structure": nm, **{k: (getattr(d, k), c.get(k)) for k in ("LCD", "PLD", "AV_VF", "ASA_m2_g")},
                    "spacing": d.spacing_A})
    return out


def _main(argv: list[str]) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m sieve.porosity")
    sub = ap.add_subparsers(dest="cmd", required=True)
    z = sub.add_parser("zeolites", help="build data/zeolites/descriptors.json")
    z.add_argument("--jobs", type=int, default=3)
    z.add_argument("codes", nargs="*")
    v = sub.add_parser("validate", help="compare with CoRE's Zeo++ values")
    v.add_argument("names", nargs="+")
    c = sub.add_parser("cif")
    c.add_argument("paths", nargs="+")
    a = ap.parse_args(argv)
    if a.cmd == "zeolites":
        rows = build_zeolites(a.jobs, a.codes or None)
        print(f"{len(rows)} zeolites in {ZEOLITE_TABLE}")
    elif a.cmd == "validate":
        for r in validate(a.names):
            print(json.dumps(r), flush=True)
    else:
        for p in a.paths:
            print(p, asdict(compute_cif(p)), flush=True)


if __name__ == "__main__":
    import sys

    _main(sys.argv[1:])

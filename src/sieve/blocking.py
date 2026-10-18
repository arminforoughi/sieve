"""Pore blocking: find pockets a CO2 molecule cannot reach from the bulk gas, and block them in RASPA.

GCMC insertion moves place molecules uniformly at random in the box, so they also fill cages whose
windows are too narrow to pass (sodalite cages of LTA/FAU behind 6-rings of ~2.3 A free diameter;
CO2's kinetic diameter is 3.3 A). Real CO2 never gets there. The standard fix is RASPA's
"BlockPockets": spheres around the inaccessible regions in which no adsorbate atom may sit.

Method (energy-based percolation, in the spirit of Haldoupis, Nair & Sholl and RASPA's own
blocking grids):
  1. Sample RASPA's O_co2 probe energy grid (the strongest LJ site of TraPPE CO2) on a grid that is
     regular in FRACTIONAL coordinates of the unit cell (RASPA tabulates a Cartesian box around the
     cell; for non-orthogonal cells that box holds more than one cell, so we interpolate it at
     r = s . H, s in [0,1)^3, H the RASPA cell matrix with a along x and b in the xy-plane).
  2. Accessible: E_probe < threshold. Default 15 k_B T at 298 K (~37 kJ/mol), the usual energy
     cut-off for "a molecule can be here at all" (Boltzmann factor e^-15 ~ 3e-7). It is a parameter.
  3. Connected components with periodic 6-connectivity: scipy.ndimage.label inside the cell, then a
     weighted union-find over the faces that tracks the lattice image shift of every merge. A
     component that meets a periodic image of itself (non-zero net shift around a loop) percolates:
     it is a channel system open to the gas. Every other component is an inaccessible pocket.
  4. Cover each pocket with spheres (k-means on its unwrapped points, radius = farthest point plus
     half a voxel diagonal), splitting until no sphere reaches a percolating accessible point.
  5. Write RASPA's .block file: number of spheres, then "x y z r" per line, x y z FRACTIONAL
     coordinates of the unit cell and r in Angstrom (RASPA framework.c ReadBlockingPockets: it adds
     ShiftUnitCell, replicates the centres over every unit cell of the supercell, converts ABC->XYZ,
     and BlockedPocket() rejects any trial position of any atom within r, minimum image per unit cell).

No pockets -> no file; blocking stays off for that structure.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

T_REF = 298.0
DEFAULT_THRESHOLD_KT = 15.0
METHOD = ("energy-based percolation: O_co2 probe energy < {thr:g} k_B T ({kj:.1f} kJ/mol at 298 K) is "
          "accessible; periodic 6-connected components; non-percolating components are blocked with "
          "RASPA BlockPockets spheres")


# --------------------------------------------------------------------------------------- geometry

def raspa_cell(a: float, b: float, c: float, alpha: float, beta: float, gamma: float) -> np.ndarray:
    """Rows = a, b, c lattice vectors in RASPA's orientation (framework.c CellProperties)."""
    al, be, ga = (math.radians(x) for x in (alpha, beta, gamma))
    t = (math.cos(al) - math.cos(ga) * math.cos(be)) / math.sin(ga)
    return np.array([[a, 0.0, 0.0],
                     [b * math.cos(ga), b * math.sin(ga), 0.0],
                     [c * math.cos(be), c * t, c * math.sqrt(max(0.0, 1.0 - math.cos(be) ** 2 - t * t))]])


def cell_from_cif(path: str | os.PathLike) -> np.ndarray:
    """RASPA cell matrix from the _cell_* lines of a (prepared P1) CIF."""
    vals = {}
    for line in Path(path).read_text().splitlines():
        p = line.split()
        if len(p) >= 2 and p[0].startswith("_cell_"):
            try:
                vals[p[0]] = float(p[1].split("(")[0])
            except ValueError:
                pass
    k = ("_cell_length_a", "_cell_length_b", "_cell_length_c", "_cell_angle_alpha", "_cell_angle_beta",
         "_cell_angle_gamma")
    return raspa_cell(*(vals[x] for x in k))


def is_orthogonal(cell: np.ndarray, tol: float = 1e-6) -> bool:
    return bool(np.all(np.abs(cell - np.diag(np.diag(cell))) < tol))


def frac_energy(grid: dict, cell: np.ndarray, spacing: float | None = None) -> np.ndarray:
    """Probe energy (K) on a grid regular in fractional coordinates, s_i = idx_i / n_i (endpoint excluded).

    grid: read_grid() output (energy_K over RASPA's Cartesian box: point (i,j,k) at shift + idx*size/n).
    Orthogonal cells with zero shift sample the RASPA grid nodes exactly; otherwise trilinear."""
    e = np.asarray(grid["energy_K"], dtype=np.float64)
    n_c, size, shift = np.asarray(grid["n"]), np.asarray(grid["size"], float), np.asarray(grid["shift"], float)
    sp = spacing or float(grid["spacing"])
    if is_orthogonal(cell) and np.allclose(shift, 0.0) and np.allclose(np.diag(cell), size, atol=1e-6):
        return e[:-1, :-1, :-1].copy()
    from scipy.ndimage import map_coordinates

    lengths = np.linalg.norm(cell, axis=1)
    n = np.maximum(8, (lengths / sp).astype(int))
    s = np.stack(np.meshgrid(*(np.arange(m) / m for m in n), indexing="ij"), axis=-1)
    r = s @ cell
    idx = (r - shift) / (size / n_c)
    idx = np.clip(idx, 0.0, n_c.astype(float))
    e = np.minimum(e, 1e7)  # RASPA's overlap values are astronomically large; keep interpolation sane
    out = map_coordinates(e, idx.reshape(-1, 3).T, order=1, mode="nearest")
    return out.reshape(tuple(n))


# ---------------------------------------------------------------------------- periodic components

class _UF:
    """Union-find where every node carries its lattice-image offset relative to its parent."""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.off = [np.zeros(3, dtype=int) for _ in range(n)]
        self.cycles: dict[int, list[np.ndarray]] = {}

    def find(self, x: int) -> tuple[int, np.ndarray]:
        path = []
        while self.parent[x] != x:
            path.append(x)
            x = self.parent[x]
        root, acc = x, np.zeros(3, dtype=int)
        for y in reversed(path):  # compress; off[y] becomes relative to root
            acc = acc + self.off[y]
            self.off[y] = acc.copy()
            self.parent[y] = root
        return root, (self.off[path[0]] if path else np.zeros(3, dtype=int))

    def union(self, u: int, v: int, shift: np.ndarray) -> None:
        """Constraint: image(v) = image(u) + shift."""
        ru, pu = self.find(u)
        rv, pv = self.find(v)
        if ru == rv:
            c = pu + shift - pv
            if np.any(c):
                self.cycles.setdefault(ru, []).append(c)
            return
        self.parent[rv] = ru
        self.off[rv] = pu + shift - pv
        if rv in self.cycles:
            self.cycles.setdefault(ru, []).extend(self.cycles.pop(rv))


def periodic_components(acc: np.ndarray) -> tuple[np.ndarray, dict]:
    """Label a boolean periodic grid. Returns (labels, info): labels 0 = inaccessible, 1..K components
    after periodic merging; info[k] = {"percolating": bool, "dims": rank of the cycle shifts,
    "offsets": {raw_label: image offset}, "raw": [raw labels]}."""
    from scipy import ndimage

    raw, nraw = ndimage.label(acc)  # 6-connectivity (faces only): never leaks along a diagonal
    uf = _UF(nraw + 1)
    for ax in range(3):
        a = np.take(raw, -1, axis=ax)
        b = np.take(raw, 0, axis=ax)
        m = (a > 0) & (b > 0)
        if not m.any():
            continue
        pairs = np.unique(np.stack([a[m], b[m]], axis=1), axis=0)
        sh = np.zeros(3, dtype=int)
        sh[ax] = 1
        for u, v in pairs:
            uf.union(int(u), int(v), sh)
    roots: dict[int, int] = {}
    lut = np.zeros(nraw + 1, dtype=np.int32)
    info: dict[int, dict] = {}
    for x in range(1, nraw + 1):
        r, p = uf.find(x)
        k = roots.setdefault(r, len(roots) + 1)
        lut[x] = k
        d = info.setdefault(k, {"root": r, "offsets": {}, "raw": []})
        d["offsets"][x] = p.copy()
        d["raw"].append(x)
    for k, d in info.items():
        cyc = uf.cycles.get(d["root"], [])
        d["dims"] = int(np.linalg.matrix_rank(np.array(cyc))) if cyc else 0
        d["percolating"] = d["dims"] > 0
    return lut[raw], {"raw": raw, "components": info}


# --------------------------------------------------------------------------------- sphere covering

def _min_image_dist(frac_pts: np.ndarray, frac_c: np.ndarray, cell: np.ndarray) -> np.ndarray:
    d = frac_pts - frac_c
    d -= np.rint(d)  # RASPA's ApplyBoundaryConditionUnitCell does the same for triclinic cells
    return np.linalg.norm(d @ cell, axis=1)


def _kmeans(x: np.ndarray, k: int, iters: int = 30, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    c = x[rng.choice(len(x), k, replace=False)]
    for _ in range(iters):
        lab = np.argmin(((x[:, None, :] - c[None]) ** 2).sum(-1), axis=1)
        new = np.array([x[lab == j].mean(0) if np.any(lab == j) else c[j] for j in range(k)])
        if np.allclose(new, c):
            break
        c = new
    return np.argmin(((x[:, None, :] - c[None]) ** 2).sum(-1), axis=1)


def cover(points_frac: np.ndarray, cell: np.ndarray, open_frac: np.ndarray, voxel: float,
          max_spheres: int = 8) -> tuple[list[dict], bool]:
    """Spheres covering one pocket's (unwrapped) points without reaching any open (percolating) point.
    Returns (spheres, clean): clean False if radii had to be trimmed short of full coverage."""
    cart = points_frac @ cell
    margin = 0.5 * voxel * math.sqrt(3)
    sub = open_frac[::max(1, len(open_frac) // 200000)] if len(open_frac) else open_frac
    best = None
    for k in range(1, min(max_spheres, len(cart)) + 1):
        lab = _kmeans(cart, k) if k > 1 else np.zeros(len(cart), dtype=int)
        out, ok = [], True
        for j in range(k):
            pc = cart[lab == j]
            if not len(pc):
                continue
            c = pc.mean(0)
            r = float(np.max(np.linalg.norm(pc - c, axis=1))) + margin
            cf = np.linalg.solve(cell.T, c)
            gap = float(_min_image_dist(sub, cf, cell).min()) if len(sub) else math.inf
            out.append({"frac": cf, "r": r, "gap": gap})
            ok &= gap > r
        if ok:
            best = (out, True)
            break
        best = (out, False)
    out, clean = best
    if not clean:  # trim: a sphere must never block the open channels
        for s in out:
            s["r"] = min(s["r"], max(s["gap"] - 0.05, 0.0))
    return [{"frac": [float(v % 1.0) for v in s["frac"]], "r": round(float(s["r"]), 3)} for s in out
            if s["r"] > 0.0], clean


# -------------------------------------------------------------------------------------- analysis

def analyse(grid: dict, cell: np.ndarray, threshold_kt: float = DEFAULT_THRESHOLD_KT,
            T: float = T_REF, min_points: int = 1) -> dict:
    """Find inaccessible pockets in a RASPA probe grid. Returns the summary dict (see blocks())."""
    e = frac_energy(grid, cell)
    thr_K = threshold_kt * T
    acc = e < thr_K
    labels, meta = periodic_components(acc)
    comps = meta["components"]
    n = np.array(e.shape)
    voxel = float(np.max(np.linalg.norm(cell, axis=1) / n))
    idx_all = np.indices(e.shape).reshape(3, -1).T
    open_mask = np.zeros(e.shape, bool)
    for k, d in comps.items():
        if d["percolating"]:
            open_mask |= labels == k
    open_frac = idx_all[open_mask.reshape(-1)] / n
    pockets, spheres, clean_all = [], [], True
    raw = meta["raw"]
    for k, d in sorted(comps.items()):
        if d["percolating"]:
            continue
        pts = []
        for x in d["raw"]:
            ii = np.argwhere(raw == x)
            pts.append(ii / n + d["offsets"][x])  # unwrapped into one contiguous image
        pts = np.concatenate(pts)
        if len(pts) < min_points:
            continue
        sph, clean = cover(pts, cell, open_frac, voxel)
        clean_all &= clean
        cf = pts.mean(0)
        emin = float(e[labels == k].min())
        pockets.append({"centre_frac": [round(float(v % 1.0), 4) for v in cf],
                        "centre_cart": [round(float(v), 3) for v in (cf % 1.0) @ cell],
                        "n_points": int(len(pts)), "volume_A3": round(len(pts) * abs(np.linalg.det(cell)) / n.prod(), 2),
                        "min_kjmol": round(emin * 0.0083145, 2), "spheres": len(sph)})
        spheres += sph
    n_acc = int(acc.sum())
    n_blk = int(sum(p["n_points"] for p in pockets))
    for s in spheres:
        s["cart"] = [round(float(v), 3) for v in np.array(s["frac"]) @ cell]
        s["frac"] = [round(v, 6) for v in s["frac"]]
    dims = sorted({d["dims"] for d in comps.values() if d["percolating"]})
    return {"enabled": bool(spheres), "n_pockets": len(pockets), "n_spheres": len(spheres),
            "blocked_fraction": round(n_blk / n_acc, 4) if n_acc else 0.0,  # of the accessible volume
            "blocked_volume_fraction": round(n_blk / acc.size, 4),         # of the cell
            "accessible_fraction": round(n_acc / acc.size, 4),
            "percolating_components": sum(1 for d in comps.values() if d["percolating"]),
            "channel_dims": dims, "clean_cover": bool(clean_all),
            "threshold_kt": threshold_kt, "threshold_kjmol": round(thr_K * 0.0083145, 2),
            "grid_frac": [int(x) for x in n], "orthogonal": is_orthogonal(cell),
            "cell": [[round(float(v), 6) for v in row] for row in cell],
            "method": METHOD.format(thr=threshold_kt, kj=thr_K * 0.0083145),
            "pockets": pockets, "spheres": spheres}


def format_block(spheres: list[dict]) -> str:
    """RASPA .block text: count, then fractional x y z and radius (A) per line."""
    lines = [str(len(spheres))]
    lines += [f"{s['frac'][0]:.6f} {s['frac'][1]:.6f} {s['frac'][2]:.6f} {s['r']:.4f}" for s in spheres]
    return "\n".join(lines) + "\n"


def parse_block(text: str) -> list[dict]:
    rows = text.split("\n")
    n = int(rows[0].strip())
    out = []
    for r in rows[1:1 + n]:
        x, y, z, rad = (float(v) for v in r.split()[:4])
        out.append({"frac": [x, y, z], "r": rad})
    return out


def blocked_mask(points_cart: np.ndarray, cell: np.ndarray, spheres: list[dict]) -> np.ndarray:
    """True for Cartesian points inside any block sphere (unit-cell minimum image, as RASPA)."""
    m = np.zeros(len(points_cart), bool)
    if not spheres:
        return m
    frac = np.linalg.solve(cell.T, points_cart.T).T
    for s in spheres:
        m |= _min_image_dist(frac, np.array(s["frac"]), cell) < s["r"]
    return m


# ----------------------------------------------------------------------------------------- cache

def block_dir(ff_name: str) -> Path:
    from sieve.oracle import PROJECT_ROOT

    return PROJECT_ROOT / "data" / "blocks" / ff_name


def write(alias: str, summary: dict, ff_name: str, root: Path | None = None) -> Path | None:
    """Cache <alias>.json (always) and <alias>.block (only when there are spheres). Returns the block
    path or None."""
    d = root or block_dir(ff_name)
    d.mkdir(parents=True, exist_ok=True)
    js, blk = d / f"{alias}.json", d / f"{alias}.block"
    tmp = js.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary))
    os.replace(tmp, js)
    if summary["spheres"]:
        tmp = blk.with_suffix(".btmp")
        tmp.write_text(format_block(summary["spheres"]))
        os.replace(tmp, blk)
        return blk
    blk.unlink(missing_ok=True)
    return None


def cached(alias: str, ff_name: str, root: Path | None = None) -> tuple[dict | None, Path | None]:
    d = root or block_dir(ff_name)
    js, blk = d / f"{alias}.json", d / f"{alias}.block"
    if not js.exists():
        return None, None
    s = json.loads(js.read_text())
    return s, (blk if s.get("spheres") and blk.exists() else None)


def from_grid_file(grid_path: Path, cif_path: str | os.PathLike, **kw) -> dict:
    from sieve.science import read_grid

    return analyse(read_grid(Path(grid_path)), cell_from_cif(cif_path), **kw)


def ensure_for_grid_dir(alias: str, grid_dir: Path, cif_path: str | os.PathLike, ff_name: str) -> Path | None:
    """Opt-in screening path: block file for a structure whose O_co2 grid is in the RASPA grid cache."""
    s, blk = cached(alias, ff_name)
    if s is not None:
        return blk
    g = next(Path(grid_dir).glob("*_O_co2_shifted.grid"), None)
    if g is None:
        return None
    return write(alias, from_grid_file(g, cif_path), ff_name)

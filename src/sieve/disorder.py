"""Disorder check: find atoms a cleaned CIF keeps on top of each other, and resolve them.

CoRE MOF "clean" CIFs resolve partial occupancies by keeping every alternative position at full
occupancy. Where a crystallographer recorded one site as "N or O" (a linker that binds either way
round) the file then holds both atoms 0.45 A apart. To GCMC those are two real atoms: the extra
Lennard-Jones centres sit exactly where CO2 binds and deepen the pockets. ZADREE_clean, the best
structure any arm found, has 32 such N/O pairs, one at every Cu donor site.

  scan(cif)       clash pairs closer than any real bond; a structure with any is "disordered"
  resolve(cif)    drop one atom of every overlapping pair, closest pairs first, keeping the one
                  with the smaller UFF epsilon, so resolving never adds attraction: if the
                  resolved structure still wins, the win is not an artifact of the doubled atoms.

The cutoffs are below every real bond length (shortest heavy-atom bonds ~1.1 A, X-H ~0.95 A in
X-ray structures), so ordinary structures report no clashes.
"""

from __future__ import annotations

import json
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from sieve import forcefield
from sieve.oracle import PROJECT_ROOT, _load_structure

HEAVY_A = 1.0     # heavy-heavy pairs closer than this are two copies of one site
WITH_H_A = 0.75   # pairs involving hydrogen
RESOLVED_DIR = PROJECT_ROOT / "data" / "cifs-resolved"
SCAN_PATH = PROJECT_ROOT / "data" / "disorder.jsonl"


def _limit(a: str, b: str) -> float:
    return WITH_H_A if "H" in (a, b) else HEAVY_A


def _clashes(s) -> list[tuple[int, int, float]]:
    i, j, _, d = s.get_neighbor_list(HEAVY_A)
    out = []
    for a, b, r in zip(i, j, d):
        if a < b and r < _limit(s[int(a)].specie.symbol, s[int(b)].specie.symbol):
            out.append((int(a), int(b), float(r)))
    return out


def scan(cif: str | Path) -> dict:
    """Clash summary for one CIF. Never raises: a parse failure is reported, not hidden."""
    name = Path(cif).stem
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = _load_structure(cif)
        pairs = _clashes(s)
    except Exception as e:  # noqa: BLE001
        return {"structure": name, "ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    kinds: dict[str, int] = {}
    for a, b, _ in pairs:
        k = "-".join(sorted((s[a].specie.symbol, s[b].specie.symbol)))
        kinds[k] = kinds.get(k, 0) + 1
    atoms = {a for p in pairs for a in p[:2]}
    return {"structure": name, "ok": True, "n_atoms": len(s), "n_clashes": len(pairs),
            "clash_atom_fraction": round(len(atoms) / max(1, len(s)), 4), "kinds": kinds,
            "min_distance_A": round(min((r for *_, r in pairs), default=0.0), 3) or None,
            "disordered": bool(pairs)}


def resolve(cif: str | Path, out_dir: Path = RESOLVED_DIR) -> tuple[Path, dict]:
    """Write <stem>_dres.cif with one atom per clash cluster; returns (path, summary)."""
    from pymatgen.core import Structure

    cif = Path(cif)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = _load_structure(cif)
    # greedy, closest pair first: drop one atom of each overlapping pair (the one with the larger
    # UFF epsilon), until no clash is left. Chains of overlaps (a disordered ring drawn twice) lose
    # one copy, not the whole chain, which a merge-into-centroid would do.
    eps = {e: v[0] for e, v in forcefield.uff_lj().items()}
    drop: set[int] = set()
    for a, b, _ in sorted(_clashes(s), key=lambda p: p[2]):
        if a in drop or b in drop:
            continue
        ea, eb = (eps.get(s[x].specie.symbol, 1e9) for x in (a, b))
        drop.add(a if (ea, a) > (eb, b) else b)
    keep = [k for k in range(len(s)) if k not in drop]
    species = [s[k].specie.symbol for k in keep]
    coords = [s[k].frac_coords for k in keep]
    r = Structure(s.lattice, species, coords)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = cif.stem.replace("_clean", "")
    path = out_dir / f"{stem}_dres_clean.cif"
    r.to(filename=str(path), fmt="cif")
    left = _clashes(r)
    return path, {"structure": cif.stem, "resolved": path.stem, "dropped": len(drop),
                  "atoms_before": len(s), "atoms_after": len(r), "clashes_left": len(left)}


def scan_pool(cif_dir: Path = PROJECT_ROOT / "data" / "cifs", out: Path = SCAN_PATH, jobs: int = 4) -> dict:
    """Scan every CIF once; resumable (already-scanned structures are skipped)."""
    done = set()
    if out.exists():
        done = {json.loads(l)["structure"] for l in out.open()}
    todo = sorted(p for p in cif_dir.glob("*.cif") if p.stem not in done)
    with ProcessPoolExecutor(jobs) as ex, out.open("a") as f:
        for i, r in enumerate(ex.map(scan, todo, chunksize=16)):
            f.write(json.dumps(r) + "\n")
            if i % 500 == 0:
                f.flush()
                print(f"{i}/{len(todo)}", flush=True)
    return load()


def load(path: Path = SCAN_PATH) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {r["structure"]: r for r in (json.loads(l) for l in path.open())}


if __name__ == "__main__":
    if sys.argv[1:2] == ["--pool"]:
        d = scan_pool(jobs=int(sys.argv[2]) if len(sys.argv) > 2 else 4)
        print(sum(r.get("disordered", False) for r in d.values()), "of", len(d), "have clashing atoms")
    else:
        for c in sys.argv[1:]:
            print(json.dumps(scan(c)))

"""The candidate pool every arm screens.

CoRE MOF 2019 ASR (all solvent removed), restricted to structures CO2 can enter: pore-limiting
diameter >= 3.3 A, CO2's kinetic diameter, from CoRE's published Zeo++ descriptors. GCMC without
pocket blocking would otherwise count CO2 placed in pores no molecule could reach, and those fake
uptakes would win the screen. (Isolated pockets inside accessible structures are still not
blocked; that is a known limitation, shared equally by all arms.)

Pools (run config "pool"; the default is MOF-only, so earlier runs and benchmarks are unchanged):
  mof      CoRE MOF 2019 ASR, PLD >= 3.3 A                        family "MOF"
  mixed    the above plus the IZA-SC zeolites with PLD >= 3.3 A  family "zeolite"
Zeolite descriptors come from sieve.porosity (grid method, the same definitions and radii as
CoRE's Zeo++ numbers; see its validation), via data/zeolites/descriptors.json. The zeolites are
pure-silica frameworks with no cations: this model treats them fairly only geometrically.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from sieve.oracle import PROJECT_ROOT, core_descriptors, zeolite_cif, zeolite_code, zeolite_codes

CIF_DIR = PROJECT_ROOT / "data" / "cifs"
CO2_KINETIC_DIAMETER_A = 3.3
POOLS = ("mof", "mixed")
MOF, ZEOLITE = "MOF", "zeolite"


@dataclass(frozen=True)
class Candidate:
    name: str
    cif_path: str
    family: str = MOF


def family_of(name: str) -> str:
    return ZEOLITE if zeolite_code(name) else MOF


def pool_kind(config: dict) -> str:
    """'mixed' if a run config asks for the mixed pool, else 'mof' (every earlier run)."""
    return "mixed" if str((config or {}).get("pool", "")).lower().startswith("mixed") else "mof"


def load_zeolites(min_pld: float = CO2_KINETIC_DIAMETER_A) -> list[Candidate]:
    from sieve.porosity import load_zeolite_table

    table = load_zeolite_table()
    out = []
    for code in zeolite_codes():
        name = f"IZA-{code}"
        pld = (table.get(name) or {}).get("PLD")
        if pld is not None and pld >= min_pld:
            out.append(Candidate(name, str(zeolite_cif(name)), ZEOLITE))
    return out


def load_candidates(cif_dir: Path = CIF_DIR, min_pld: float = CO2_KINETIC_DIAMETER_A,
                    pool: str = "mof") -> list[Candidate]:
    core = core_descriptors()
    out = []
    for p in sorted(Path(cif_dir).glob("*.cif")):
        pld = (core.get(p.stem) or {}).get("PLD")
        if pld is not None and pld >= min_pld:
            out.append(Candidate(p.stem, str(p)))
    if pool == "mixed":
        out += load_zeolites(min_pld)
    elif pool != "mof":
        raise ValueError(f"unknown pool {pool!r} (one of {POOLS})")
    return out


def sample(cands: list[Candidate], limit: int | None, seed: int) -> list[Candidate]:
    """A random subset for smoke runs, stratified by family so a small mixed pool still holds
    zeolites: each family gets its share of `limit`, but at least a quarter of it (or all of the
    family, if smaller). A MOF-only pool samples exactly as before (random.Random(seed).sample)."""
    if not limit or limit >= len(cands):
        return list(cands)
    fams: dict[str, list[Candidate]] = {}
    for c in cands:
        fams.setdefault(c.family, []).append(c)
    rng = random.Random(seed)
    if len(fams) == 1:
        return rng.sample(cands, limit)
    small = {f: min(len(m), max(limit // 4, round(limit * len(m) / len(cands)))) for f, m in fams.items() if f != MOF}
    out = []
    for f, n in small.items():
        out += rng.sample(fams[f], n)
    mof = fams.get(MOF, [])
    out += rng.sample(mof, min(len(mof), limit - len(out)))
    return out


def candidate_names(pool: str = "mof", limit: int | None = None, seed: int = 0) -> list[str]:
    return [c.name for c in sample(load_candidates(pool=pool), limit, seed)]


# ------------------------------------------------------------------ disorder-resolved variants
# sieve.disorder.resolve() writes data/cifs-resolved/<stem>_dres_clean.cif: the same structure with
# one atom of every overlapping pair dropped. Such a variant is simulated by name like any other
# structure (it is never a pool candidate; it is the critic's disorder re-measure of one).
RESOLVED_TAG = "_dres"


def is_resolved(name: str) -> bool:
    return RESOLVED_TAG in name


def resolved_name(name: str) -> str:
    base = name[:-len("_clean")] if name.endswith("_clean") else name
    return f"{base}{RESOLVED_TAG}_clean"


def original_name(name: str) -> str:
    """"ZADREE_dres_clean" -> "ZADREE_clean"; "EXOTUH_SL_dres_clean" -> "EXOTUH_SL"."""
    tail = f"{RESOLVED_TAG}_clean"
    if not name.endswith(tail):
        return name.replace(RESOLVED_TAG, "")
    base = name[:-len(tail)]
    return f"{base}_clean" if (CIF_DIR / f"{base}_clean.cif").exists() or not (CIF_DIR / f"{base}.cif").exists() \
        else base


def resolved_cif(name: str, cif_dir: Path = CIF_DIR) -> Path:
    """The resolved CIF for a resolved name ("ZADREE_dres_clean"), written on first use."""
    from sieve.disorder import RESOLVED_DIR, resolve

    p = RESOLVED_DIR / f"{name}.cif"
    if not p.exists():
        out, _ = resolve(cif_path(original_name(name), cif_dir), out_dir=RESOLVED_DIR)
        if out != p:
            raise FileNotFoundError(f"resolve() wrote {out}, expected {p}")
    return p


def cif_path(name: str, cif_dir: Path = CIF_DIR) -> str:
    z = zeolite_cif(name)
    if z is not None:
        return str(z)
    if is_resolved(name):
        return str(resolved_cif(name, cif_dir))
    return str(Path(cif_dir) / f"{name}.cif")

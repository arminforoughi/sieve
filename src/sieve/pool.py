"""The candidate pool every arm screens.

CoRE MOF 2019 ASR (all solvent removed), restricted to structures CO2 can enter: pore-limiting
diameter >= 3.3 A, CO2's kinetic diameter, from CoRE's published Zeo++ descriptors. GCMC without
pocket blocking would otherwise count CO2 placed in pores no molecule could reach, and those fake
uptakes would win the screen. (Isolated pockets inside accessible structures are still not
blocked; that is a known limitation, shared equally by all arms.)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sieve.oracle import PROJECT_ROOT, core_descriptors

CIF_DIR = PROJECT_ROOT / "data" / "cifs"
CO2_KINETIC_DIAMETER_A = 3.3


@dataclass(frozen=True)
class Candidate:
    name: str
    cif_path: str


def load_candidates(cif_dir: Path = CIF_DIR, min_pld: float = CO2_KINETIC_DIAMETER_A
                    ) -> list[Candidate]:
    core = core_descriptors()
    out = []
    for p in sorted(Path(cif_dir).glob("*.cif")):
        pld = (core.get(p.stem) or {}).get("PLD")
        if pld is not None and pld >= min_pld:
            out.append(Candidate(p.stem, str(p)))
    return out


def cif_path(name: str, cif_dir: Path = CIF_DIR) -> str:
    return str(Path(cif_dir) / f"{name}.cif")

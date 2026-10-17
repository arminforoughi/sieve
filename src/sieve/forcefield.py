"""SieveUFF: the RASPA force field SIEVE simulates with.

Framework: UFF Lennard-Jones for every element (data/uff_lj.csv), no partial charges.
Adsorbate: TraPPE CO2 (Potoff & Siepmann 2001); the SieveUFFMix variant adds TraPPE N2 for
CO2/N2 mixture runs. Lorentz-Berthelot mixing, 12.8 A cutoff, shifted,
no tail corrections. Electrostatics are off (ChargeMethod None): the frameworks carry no charges, so
only CO2-CO2 Coulomb would remain, which is negligible at 1 bar (checked on IRMOF-1).

Pseudo-atoms are named exactly by element symbol and prepared CIFs use bare element labels, so no
RASPA wildcard rule (`C_` matches anything starting with "C", including Cu/Ce/Cl) is ever involved.
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

NAME = "SieveUFF"
KCAL_PER_MOL_TO_K = 503.2195
TWO_POW_SIXTH = 2 ** (1 / 6)

# name: (epsilon/k_B [K], sigma [A], mass, charge)
TRAPPE_CO2 = {
    "C_co2": (27.0, 2.80, 12.0107, 0.70),
    "O_co2": (79.0, 3.05, 15.9994, -0.35),
}
GRID_TYPES = ("C_co2", "O_co2")

# TraPPE N2 (Potoff & Siepmann 2001): two LJ N sites 1.10 A apart plus a massless, LJ-free centre
# of mass that carries +0.964 e. RASPA's TraPPE/N2.def references N_com, so it must be defined. As
# with CO2 the guest charges are written for completeness but never used (ChargeMethod None).
# Only the mixture force field carries N2; SieveUFF itself stays byte-identical for the screen.
MIX_NAME = "SieveUFFMix"
TRAPPE_N2 = {
    "N_n2": (36.0, 3.31, 14.0067, -0.482),
    "N_com": (0.0, 0.0, 0.0, 0.964),
}


@lru_cache(maxsize=1)
def uff_lj() -> dict[str, tuple[float, float]]:
    """element -> (epsilon/k_B [K], sigma [A])."""
    path = Path(__file__).parent / "data" / "uff_lj.csv"
    rows = csv.DictReader(line for line in open(path) if not line.startswith("#"))
    return {r["element"]: (float(r["D1"]) * KCAL_PER_MOL_TO_K, float(r["x1"]) / TWO_POW_SIXTH)
            for r in rows}


def write(dest: Path, mixture: bool = False) -> None:
    """Write pseudo_atoms.def, force_field_mixing_rules.def and force_field.def into dest.

    mixture=True also defines TraPPE N2 (the SieveUFFMix force field for CO2/N2 runs); the CO2 and
    framework parameters are identical either way."""
    from pymatgen.core import Element

    dest.mkdir(parents=True, exist_ok=True)
    lj = uff_lj()
    pseudo = []
    for el in lj:
        try:
            mass = float(Element(el).atomic_mass)
        except Exception:
            mass = 1.0  # superheavy elements with no tabulated mass never occur in MOFs
        pseudo.append(f"{el:<8} yes {el:<3} {el:<3} 0 {mass:12.6f} 0.0 0.0 1.0 1.0 0 0 relative 0")
    for name, (_, _, mass, q) in TRAPPE_CO2.items():
        sym = name[0]
        pseudo.append(f"{name:<8} yes {sym:<3} {sym:<3} 0 {mass:12.6f} {q} 0.0 1.0 1.0 0 0 relative 0")
    n2 = TRAPPE_N2 if mixture else {}
    for name, (_, _, mass, q) in n2.items():
        pseudo.append(f"{name:<8} yes N   N   0 {mass:12.6f} {q} 0.0 1.0 1.0 0 0 relative 0")
    (dest / "pseudo_atoms.def").write_text(
        "#number of pseudo atoms\n"
        f"{len(pseudo)}\n"
        "#type print as chem oxidation mass charge polarization B-factor radii connectivity "
        "anisotropic anisotropic-type tinker-type\n" + "\n".join(pseudo) + "\n")

    inter = [f"{el:<8} lennard-jones {eps:10.5f} {sig:8.5f}" for el, (eps, sig) in lj.items()]
    inter += [f"{n:<8} lennard-jones {e:10.5f} {s:8.5f}" for n, (e, s, _, _) in TRAPPE_CO2.items()]
    inter += [f"{n:<8} lennard-jones {e:10.5f} {s:8.5f}" if e > 0 else f"{n:<8} none"
              for n, (e, s, _, _) in n2.items()]
    (dest / "force_field_mixing_rules.def").write_text(
        "# general rule for shifted vs truncated\nshifted\n"
        "# general rule tailcorrections\nno\n"
        f"# number of defined interactions\n{len(inter)}\n"
        "# type interaction, parameters\n" + "\n".join(inter) + "\n"
        "# general mixing rule for Lennard-Jones\nLorentz-Berthelot\n")
    (dest / "force_field.def").write_text(
        "# rules to overwrite\n0\n# number of defined interactions\n0\n# mixing rules to overwrite\n0\n")

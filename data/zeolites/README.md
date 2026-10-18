# IZA zeolite reference frameworks

250 zeolite framework CIFs in `iza/<CODE>.cif` (`iza/codes.txt` lists the codes asked for;
`iza/fetch.log` the download log; interrupted frameworks such as `-CLO` were not fetched).

- Source: IZA-SC **Database of Zeolite Structures**, Ch. Baerlocher and L. B. McCusker,
  http://www.iza-structure.org/databases/ . Coordinates and cell parameters are the database's
  DLS-76 optimisation assuming a pure SiO2 composition.
- **Downloaded locally, not redistributed.** `data/` is gitignored; only this README is tracked.
  To rebuild, download each framework's CIF from the IZA-SC site into `iza/`.
- Cite the database (Baerlocher & McCusker, Database of Zeolite Structures) when using numbers
  computed from these structures.

## How SIEVE uses them

- Name scheme: `IZA-<CODE>` (e.g. `IZA-MFI`). `sieve.oracle.zeolite_cif()` resolves it through a
  symlink `named/IZA-<CODE>.cif -> ../iza/<CODE>.cif`, so the structure, its RASPA alias, its
  prepared P1 CIF and its `data/science/` files are all called `IZA-<CODE>`.
- `python -m sieve.science IZA-MFI` computes the energy field and isotherm;
  `--props` adds CO2/N2 selectivity, working capacities and Q_st. The Science page finds them by
  "IZA-MFI", "zeolite MFI" or "zeolite", and offers computed ones as a "zeolite references" group.
- Parsing: the CIFs give coordinates to 4 decimals, so symmetry images of atoms on special
  positions land ~1e-4 (fractional) apart and pymatgen's default site tolerance keeps both
  (CHA: 132 atoms instead of 108; DDR: 372 instead of 360). `sieve.oracle._load_structure()`
  merges with a 1e-3 tolerance for files under `data/zeolites/` only. With that, all 250 parse to
  exact SiO2 with no Si-O contact below 1.3 A.
- They are not CoRE MOFs, so CoRE has no Zeo++ descriptors for them. `python -m sieve.porosity zeolites`
  computes PLD, LCD, void fraction, N2-probe surface area and density on CoRE's scale (grid method,
  CCDC radii; validated against CoRE on 20 MOFs, see `sieve/porosity.py`) into `descriptors.json`, which
  `core_descriptors()` merges so `--pool mixed` can screen them next to the MOFs.

## Caveat

These are **pure-silica frameworks with no extra-framework cations**. Industrial CO2 zeolites
such as 13X (NaX, FAU topology) and 5A (CaA, LTA topology) bind CO2 through their Na+/Ca2+
cations and the electric field around them. Neither these structures nor SIEVE's charge-free
force field contain that, so computed uptakes are not predictions for 13X or 5A, and any agreement
with their measured values is coincidental. They show what the topology alone gives under the same
model as the MOFs.

**No pore blocking.** RASPA inserts CO2 anywhere the energy allows, including cages whose windows
are too narrow for it to enter (6-rings, ~2.5 A): the sodalite cages of LTA and FAU. For IZA-LTA,
288 of the 392 grid points below -10 kJ/mol, and the deepest well (-15.5 kJ/mol), lie within
3.5 A of the sodalite-cage centre; the alpha cage bottoms out at about -5 kJ/mol. Its uptake is
therefore inflated relative to a blocked simulation. (The MOF pipeline has the same limitation.)

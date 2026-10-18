# SIEVE

**System map:** agents, models, methods, data and tests in one page: [docs/OVERVIEW.md](docs/OVERVIEW.md).

A fault-tolerant, multi-fidelity screen of metal-organic frameworks for CO2 capture under a fixed
compute budget. It ranks CoRE MOF 2019 structures by simulated CO2 uptake at 298 K and 1 bar, and
spends GCMC time where it can change the answer instead of everywhere.

**What the numbers are.** Absolute CO2 loading (mol/kg) from rigid-framework GCMC with a generic
force field (UFF framework Lennard-Jones, TraPPE CO2, no framework partial charges). This is a
*ranking* signal for a screen, not a prediction of measured uptake. Without charges, open-metal-site
and polar frameworks are systematically under-rewarded.

**Tier-1 numbers are not converged.** Tier 1 (500 + 100 cycles) carries roughly +/- 18% nominal
uncertainty (RASPA's own block errors on this pool were 5-10%; tier-1 vs tier-2 differences 3-7%).
Nothing is called a winner on tier-1 data; a winner needs tier-2 (2000 + 400 cycles) confirmation.

## Setup

```bash
./scripts/fetch_raspa.sh   # RASPA2 2.0.4 from PyPI + two patches (see below)
uv sync
uv run python -m sieve.oracle smoke   # prints a real uptake for IRMOF-1, direct and grid
```

CoRE MOF 2019 (CC-BY-4.0, Zenodo 10.5281/zenodo.3677685): `2019-11-01-ASR-public_12020.tar.gz`
and its CSV go in `data/raw/`, CIFs extracted flat into `data/cifs/`.

## Commands

```bash
uv run python -m sieve baseline random|descriptor|uniform --calibration data/bench/modes.db
uv run python -m sieve resume <run_id>       # continue after kill -9, nothing lost
uv run python -m sieve status [run_id]
uv run python -m sieve dashboard [--demo-replay <run_id>]   # http://127.0.0.1:8770
uv run python -m sieve.referee data/runs.db data/bench/modes.db
```

## The fidelity tiers, as measured here

| Tier | What | Cost on this machine (M1 Max, 1 core) |
|---|---|---|
| 0 | pymatgen parse, P1 CIF, descriptors + CoRE Zeo++ table | median 24 ms, 15 CPU-min for all 12,020 |
| 1 | GCMC 500 + 100 cycles | median ~27 s; 18 s to 150 s+ |
| 2 | GCMC 2000 + 400 cycles | median ~58 s with a cached grid, ~120 s direct |
| 3 | capture check (objective `capture` only): 15:85 CO2/N2 mixture at 1 bar + pure CO2 at 0.01 bar, 500 + 200 cycles each | Cu-BTC: 35 CPU-s (estimate 36 s) |

Cost is not a function of atom count alone. RASPA does max(20, N) moves per cycle, so a structure
that adsorbs a lot costs more to simulate: cost depends on the quantity being measured. The cost
model (`oracle.CostModel`) predicts N from a structure's previous result or a fitted prior and is
within 0.73-1.55x of measured CPU on the calibration set.

**Grid vs direct.** Each GCMC call either sums framework Lennard-Jones explicitly every move
("direct") or interpolates a 0.3 A energy grid built once per structure ("grid"). The grid costs
O(grid points x framework atoms) to build, then makes MC nearly independent of framework size
and is reused when a structure is promoted from tier 1 to tier 2. `Oracle.plan()` picks the cheaper.
On 9 paired measurements grid and direct agree within reported error (tier-2 differences +1.8%,
+4.8%, +0.8%, -1.6%); a cached grid makes tier 2 2-4x cheaper.

## Objectives, pools and finalist checks

All three are run-config options (stored in `runs.config_json`, restored by `resume`); the defaults
leave every earlier run, arm and benchmark exactly as it was.

- **Objective** (`--objective uptake|capture`). `capture` keeps tiers 1-2 as the screen, then ranks
  finalists by a tier-3 capture check (`sieve/capture.py`): APS = S(CO2/N2) x dN_mix, with S from a
  15:85 CO2/N2 binary GCMC at 1 bar and dN_mix = mixture CO2 loading - pure-CO2 loading at 0.01 bar
  (vacuum swing). APS as selectivity x working capacity follows Chung et al., *Sci. Adv.* 2, e1600909
  (2016). A tier-3 row is one budgeted evaluation (two RASPA calls, heartbeats, CPU limit, `lost` on a
  crash) with `uptake_molkg` NULL, so 1-bar rankings ignore it; the ledger counts it. Agents: phases
  explore (to 60% of budget) -> confirm (to 80%) -> capture; the orchestrator holds back what the final
  checks will cost; the allocator may schedule tier 3 only on tier-2-confirmed leaders; the critic
  requires it; `recommend` ranks by APS among capture-checked structures and names nothing otherwise.
  Baselines (`baseline ... --objective capture`) hold back the same funnel's cost and end with the same
  check of their own top 3 (tier 2 first where missing), recommending the best APS. No replay model.
- **Pool** (`--pool mof|mixed`). `mixed` adds the IZA-SC zeolites (pure silica, no cations). Their
  PLD/LCD/void fraction/surface area come from `sieve/porosity.py` (grid method, CoRE's definitions and
  CCDC radii; Zeo++ is not installed). On 20 CoRE MOFs vs CoRE's Zeo++ values: LCD mean |diff| 0.04 A,
  PLD -0.09 A (one-signed, calibrated out), void fraction 0.005, surface area 29% low (ranking feature
  only). The world gives material class a feature column and its own z-families; prompts say the model
  is fair to zeolites only geometrically. `--limit N` samples the pool once (family-stratified) and stores
  the sample, so a resume screens the same structures.
- **Disorder check** (on for new real runs, off for replay seeds and older runs). A finalist whose CIF
  has atoms closer than any bond (`sieve/disorder.py`) is re-measured at tier 2 on its resolved CIF
  (`<stem>_dres_clean`) before it can be recommended; the recommendation uses the resolved value, and a
  drop of more than 15% is a `disorder_artifact` that vetoes the original (`sieve/agents/checks.py`).
  `python -m sieve.referee <db> --disorder` reports raw and resolved referee values of every arm's winner.

## Fault tolerance

- **kill -9.** Every evaluation is a row; the budget ledger is the sum of measured child CPU, not a
  counter. While a job runs, the oracle heartbeats the child's CPU time every 5 s. `resume`
  marks in-flight evaluations `lost`, charges their heartbeat CPU, re-queues their tasks, and keeps
  every completed result. `tests/test_store.py::test_kill9_mid_gcmc_then_resume` really SIGKILLs
  a process mid-GCMC.
- **Orphans.** RASPA runs in a child process with a parent-death watchdog; when its parent dies it
  exits within ~1 s (checked: 9 of 9 orphans gone in 2.5 s).
- **Timeouts are CPU-time limits** enforced by the kernel (RLIMIT_CPU -> SIGXCPU), with a wall
  backstop only for hangs. A busy machine does not kill a job that is merely slow. A timeout is a
  normal result: recorded, charged, reported, the run continues.
- **Dead workers.** The supervisor reclaims a dead worker's evaluation and task and starts a
  replacement (`tests/test_workers.py`).

## Deviations from the build brief, and why

- **RASPA2 needed patching.** On macOS it does not stream (gated on `__unix__`, which Apple clang
  does not define) and crashes on long install paths (fixed 256-char buffers). Its grid builder
  wrote all-zero grids: `PotentialThirdDerivative` has no case for
  `LENNARD_JONES_CONTINUOUS_FRACTIONAL`, the type RASPA assigns every LJ pair. Patched in
  `patches/raspa2-2.0.4-sieve.patch`.
- **Force field.** The brief's `CrystalGenerator` has no parameters for lanthanides, alkali metals
  and others common in CoRE, and its wildcard rules (`C_`) would give Cu/Ce/Cl atoms carbon
  parameters. SieveUFF uses UFF for all 103 elements with exact names; each job verifies every
  framework element has a real LJ interaction with CO2.
- **No charges, no Ewald.** Agreed trade-off: 2.3x faster; IRMOF-1 uptake unchanged within error.
- **Pool filter.** Structures with pore-limiting diameter < 3.3 A (CO2's kinetic diameter) are
  excluded for every arm: without pocket blocking, GCMC places CO2 in pores no molecule can reach
  (27% of CoRE). 8,794 candidates remain. Isolated pockets in accessible structures are not
  blocked in the screening runs so far (see Pore blocking).
- **Workers = performance cores - 1** (7 on an M1 Max), not ncores - 1: the efficiency cores burn
  CPU-seconds 2-3x faster for the same work.
- **No labels.csv.** Boyd-Woo labels are for hypothetical MOFs, not CoRE, and were computed with
  charges. Arms are scored by a post-hoc referee instead (`sieve/referee.py`): fresh, independent
  tier-2 re-measurement of every arm's top 10, which also removes the winner's curse from the
  arms' own numbers.
- **Build order.** `workers.py` was written before the baselines finished, because three 4 CPU-hour
  arms need parallel workers to finish in a working day. No agent code exists until all three
  baselines have run to completion.

## Pore blocking

GCMC insertions are uniform in the box, so they also fill cages CO2 cannot reach from the gas
(the sodalite cages of LTA and FAU behind 6-rings). `sieve/blocking.py` finds them on RASPA's own
O_co2 probe grid: points below 15 k_B T are accessible, they are joined with periodic
6-connectivity (union-find over the cell faces that tracks lattice image shifts, so non-orthogonal
cells and pockets straddling the cell boundary are handled), and every component that does not
reach a periodic image of itself is a pocket. Pockets are covered with spheres and written as a
RASPA `.block` file (count, then fractional x y z and radius in A per line), cached in
`data/blocks/<forcefield>/<alias>.block` with a JSON summary; no pockets, no file.

- **Science page: on by default.** `python -m sieve.science --reblock <names>` analyses each
  structure and recomputes only those with pockets; the unblocked results are kept as
  `<alias>.noblock.json` / `<alias>.props.noblock.json` for the before/after line.
- **Screening: off by default**, so every run so far stays comparable. To use it in a future
  full run, put `"block_pockets": true` in the run's config (`runs.config_json`), or construct
  `Oracle(..., block_pockets=True)`. The oracle then blocks with the cached analysis for the
  structure or, in grid mode, one computed from the run's own O_co2 grid; a direct-mode
  evaluation without either runs unblocked and says so in its `meta.pore_blocking`.
  **This should be the setting for the next full run.**

## Results

Full-pool runs, 4 CPU-h each. Every arm's top 10 was re-measured at tier 2 by an independent
referee.

| Structure | Found by | Referee, 1 bar (mol/kg) | Crystal file |
|---|---|---|---|
| ZADREE | random, uniform | 9.41 ± 0.04 | disordered (doubled atoms at the Cu sites) |
| GANBAZ01 | descriptor | 8.16 ± 0.05 | disordered |
| WARFAY04 | random | 8.16 ± 0.07 | disordered |
| **QIPROY** | **SIEVE** | **7.15 ± 0.06** | clean |

- **Highest uptake:** SIEVE did not find the highest-uptake structure. On the 300-structure arena
  benchmark it does not yet beat random search (median winner 4.6 vs 5.2 mol/kg at 1 CPU-h).
- **Clean files:** the three structures above SIEVE's pick all have broken crystal files. ZADREE
  re-measured on a corrected file still holds 9.82 ± 0.12 mol/kg at 1 bar (5.98 at 0.15 bar,
  down 14%), so it stays the best structure found. Ranks 2 and 3 are not yet re-measured.
- **More detail:** the comparison with MEA, 13X, HKUST-1 and Mg-MOF-74, and why it matters, is in
  [docs/OVERVIEW.md](docs/OVERVIEW.md#9-what-we-found-compared-with-standard-capture-materials).

## License

MIT, see [LICENSE](LICENSE). The Nemotron-Science-v1 excerpt in `data/library/` is CC-BY-4.0 (NVIDIA); RASPA2 is MIT.

# SIEVE

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
  (27% of CoRE). 8,794 candidates remain. Isolated pockets in accessible structures are still
  not blocked.
- **Workers = performance cores - 1** (7 on an M1 Max), not ncores - 1: the efficiency cores burn
  CPU-seconds 2-3x faster for the same work.
- **No labels.csv.** Boyd-Woo labels are for hypothetical MOFs, not CoRE, and were computed with
  charges. Arms are scored by a post-hoc referee instead (`sieve/referee.py`): fresh, independent
  tier-2 re-measurement of every arm's top 10, which also removes the winner's curse from the
  arms' own numbers.
- **Build order.** `workers.py` was written before the baselines finished, because three 4 CPU-hour
  arms need parallel workers to finish in a working day. No agent code exists until all three
  baselines have run to completion.

## Results

Baselines are running. This section is filled in from `sieve status` and the referee report,
including if SIEVE loses to the descriptor-ranked baseline.

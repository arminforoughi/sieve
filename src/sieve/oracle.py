"""The oracle: the only module that runs simulations or reads labels.csv.

Three fidelity tiers behind one interface:
  tier 0  pymatgen parse -> descriptors + a RASPA-ready P1 CIF (proxy only, no simulation)
  tier 1  GCMC, 500 cycles + 100 init   (nominal +/- 18%, NOT converged)
  tier 2  GCMC, 2000 cycles + 400 init  (nominal +/- 8.6%)

A tier-1/2 call runs either "direct" (framework LJ summed explicitly every move) or "grid"
(framework energy tabulated once per structure at 0.3 A, then interpolated). The grid costs
O(grid points x framework atoms) to build but makes MC nearly independent of framework size, and
it is reused by later calls on the same structure (tier 1 -> tier 2). plan() picks the cheaper.

Every call is logged to SQLite (`evaluations`, plus `timeouts` on a kill). The budget ledger is
*derived* from that log -- spent = sum of measured child CPU-seconds -- so there is no separate
counter that can drift from the record after a crash.

Uptake is ABSOLUTE CO2 loading in mol/kg at 298 K / 1 bar under SieveUFF (UFF framework, TraPPE
CO2, no framework charges). Use it to RANK structures, not as a prediction of measured uptake.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from sieve import forcefield

CUTOFF_A = 12.8
DEFAULT_BUDGET_CPU_S = 4 * 3600.0
GRID_SPACING_A = 0.3  # IRMOF-1: 0.661 +/- 0.058 (0.3 A grid) vs 0.696 +/- 0.079 direct
GRID_CACHE_CAP_BYTES = int(float(os.environ.get("SIEVE_GRID_CACHE_GB", "1.0")) * 1e9)
GRID_PIN_S = 900.0  # never evict a grid used in the last 15 minutes
HEARTBEAT_S = 5.0
TIMEOUT_FACTOR = 4.0  # CPU limit = 4x estimate (bounded by the tier's max)
WALL_BACKSTOP = 6.0   # wall-clock kill only if a job is stuck, not merely slow
CACHE_ROOT = Path(os.environ.get("SIEVE_CACHE", Path.home() / ".cache" / "sieve"))
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORE_CSV = PROJECT_ROOT / "data" / "raw" / "2019-11-01-ASR-public_12020.csv"


@dataclass(frozen=True)
class Tier:
    id: int
    cycles: int
    init_cycles: int
    nominal_rel_err: float
    max_timeout_s: float


TIERS = {
    1: Tier(1, cycles=500, init_cycles=100, nominal_rel_err=0.18, max_timeout_s=240.0),
    2: Tier(2, cycles=2000, init_cycles=400, nominal_rel_err=0.086, max_timeout_s=900.0),
}


MIN_STEPS_PER_CYCLE = 20  # RASPA: one cycle = max(20, N molecules) MC moves


@dataclass(frozen=True)
class CostModel:
    """CPU-second model; priors calibrated on IRMOF-1 on one M1 Max performance core.

    A cycle is max(20, N) moves, so cost scales with how much CO2 the structure holds -- the
    quantity being measured. N is predicted from this structure's earlier result if there is one,
    else from uptake_prior. A direct move loops over every framework atom; a grid move does not.
    """
    direct_a: float = 0.5
    direct_k: float = 1.14e-6   # per (move x framework atom in the simulated supercell)
    grid_g: float = 7.0e-8      # build: per (grid point x supercell atom x grid type)
    grid_mc_a: float = 0.5
    grid_mc_k: float = 4.4e-4   # per move
    uptake_prior: float = 1.5   # mol/kg, used to predict N before a structure is first measured
    n_fits: int = 0

    @staticmethod
    def molecules(desc: "Descriptors", uptake_molkg: float) -> float:
        cells = desc.unit_cells[0] * desc.unit_cells[1] * desc.unit_cells[2]
        grams_per_mol = desc.density * desc.volume * cells * 0.602214  # rho * V * N_A
        return uptake_molkg * grams_per_mol / 1000.0

    def moves(self, desc: "Descriptors", tier: int, uptake_molkg: float | None) -> float:
        t = TIERS[tier]
        n = self.molecules(desc, uptake_molkg if uptake_molkg is not None else self.uptake_prior)
        return (t.cycles + t.init_cycles) * max(MIN_STEPS_PER_CYCLE, n)


SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    eval_id      TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    structure    TEXT NOT NULL,
    tier         INTEGER NOT NULL,
    mode         TEXT,                   -- describe | direct | grid
    status       TEXT NOT NULL,          -- running | ok | timeout | error
    est_cpu_s    REAL,
    timeout_s    REAL,
    cpu_s        REAL,                   -- measured user+sys of the job processes; debits budget
    wall_s       REAL,
    uptake_molkg REAL,                   -- absolute loading, mol/kg
    uptake_err   REAL,                   -- RASPA block-average error on the above
    result_json  TEXT,                   -- descriptors / per-phase costs / error text
    worker_id    TEXT,
    started_at   REAL NOT NULL,
    heartbeat_at REAL,                   -- updated every few s while running
    heartbeat_cpu_s REAL,                -- CPU consumed as of heartbeat_at; charged if lost
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS ix_eval_run ON evaluations(run_id, status);
CREATE TABLE IF NOT EXISTS timeouts (
    eval_id      TEXT PRIMARY KEY REFERENCES evaluations(eval_id),
    run_id       TEXT NOT NULL,
    structure    TEXT NOT NULL,
    tier         INTEGER NOT NULL,
    phase        TEXT NOT NULL,          -- makegrid | mc_direct | mc_grid
    timeout_s    REAL NOT NULL,          -- CPU-second limit that was hit
    n_atoms_sim  INTEGER,
    killed_at    REAL NOT NULL
);
"""


class BudgetExhausted(Exception):
    pass


@dataclass
class Descriptors:
    structure: str
    parse_ok: bool
    alias: str = ""             # RASPA-safe framework name
    prepared_path: str = ""     # P1 CIF with bare element labels
    n_atoms: int = 0
    n_atoms_sim: int = 0        # atoms in the supercell actually simulated
    unit_cells: tuple[int, int, int] = (1, 1, 1)
    density: float = 0.0        # g/cm3
    volume: float = 0.0         # A3, unit cell
    perp_widths: tuple[float, float, float] = (0.0, 0.0, 0.0)
    void_fraction_geo: float = 0.0  # crude: 1 - sum(vdW sphere volumes)/V, overlaps ignored
    elements: list[str] = field(default_factory=list)
    metals: list[str] = field(default_factory=list)
    core: dict = field(default_factory=dict)  # CoRE MOF 2019 Zeo++ descriptors, when listed
    error: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Descriptors":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        for k in ("unit_cells", "perp_widths"):
            if k in known:
                known[k] = tuple(known[k])
        return cls(**known)


@dataclass
class Plan:
    mode: str               # direct | grid
    build_grid: bool
    est_cpu_s: float
    est_direct_s: float
    est_grid_s: float


@dataclass
class Evaluation:
    eval_id: str
    structure: str
    tier: int
    mode: str
    status: str             # ok | timeout | error
    uptake_molkg: float | None
    uptake_err: float | None
    cpu_s: float
    wall_s: float
    timeout_s: float | None
    est_cpu_s: float | None
    result: dict

    @property
    def nominal_rel_err(self) -> float:
        return TIERS[self.tier].nominal_rel_err


METALS = {
    "Li", "Be", "Na", "Mg", "Al", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu",
    "Zn", "Ga", "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn",
    "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb",
    "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Th", "U", "Np",
    "Pu", "Am",
}


# ---------------------------------------------------------------------------------- RASPA env

def raspa_package_dir() -> Path:
    import RASPA2

    return Path(RASPA2.__file__).resolve().parent


def bundled_cif_dir() -> Path:
    return raspa_package_dir() / "share/raspa/structures/cif"


class RaspaEnv:
    """A RASPA_DIR tree under ~/.cache/sieve.

    libraspa builds paths in fixed char[256] buffers, so the tree must live at a short path (a
    ~230-char venv path overflowed them and macOS FORTIFY killed the process with SIGTRAP).
    Shared read-only data is symlinked from the package; grids are a real, shared cache.
    Grids are built in a private tree and renamed into the cache, so a kill mid-build never
    leaves a half-written grid where another job could read it.
    """

    def __init__(self, root: Path = CACHE_ROOT):
        self.root = root / "raspa"
        self.jobs = root / "jobs"
        self.share = self.root / "share" / "raspa"
        self.grids = self.share / "grids" / forcefield.NAME
        self._ensure()

    def _ensure(self) -> None:
        pkg = raspa_package_dir() / "share" / "raspa"
        self.share.mkdir(parents=True, exist_ok=True)
        self.grids.mkdir(parents=True, exist_ok=True)
        self.jobs.mkdir(parents=True, exist_ok=True)
        for d in ("molecules", "structures", "framework"):
            _symlink(pkg / d, self.share / d)
        ff = self.share / "forcefield"
        ff.mkdir(exist_ok=True)
        for d in (pkg / "forcefield").iterdir():
            _symlink(d, ff / d.name)
        mine = ff / forcefield.NAME
        if not (mine / "pseudo_atoms.def").exists():
            tmp = Path(tempfile.mkdtemp(dir=ff, prefix=".ff-"))
            forcefield.write(tmp)
            try:
                os.rename(tmp, mine)
            except OSError:  # another process won the race
                shutil.rmtree(tmp, ignore_errors=True)

    def grid_dir(self, alias: str) -> Path:
        return self.grids / alias / f"{GRID_SPACING_A:f}"  # RASPA formats spacing with %lf

    def has_grid(self, alias: str) -> bool:
        d = self.grid_dir(alias)
        return all((d / f"{alias}_{t}_shifted.grid").exists() for t in forcefield.GRID_TYPES)

    def touch_grid(self, alias: str) -> None:
        try:
            os.utime(self.grid_dir(alias))
        except FileNotFoundError:
            pass

    def private_root(self) -> Path:
        root = Path(tempfile.mkdtemp(dir=self.jobs, prefix="g-"))
        share = root / "share" / "raspa"
        (share / "grids").mkdir(parents=True)
        for d in ("molecules", "structures", "framework", "forcefield"):
            os.symlink(self.share / d, share / d)
        return root

    def publish(self, private: Path, alias: str) -> bool:
        built = private / "share/raspa/grids" / forcefield.NAME / alias / f"{GRID_SPACING_A:f}"
        if not built.is_dir():
            return False
        dest = self.grid_dir(alias)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(built, dest)
        except OSError:  # already published by a concurrent job
            pass
        return self.has_grid(alias)

    def evict(self, cap_bytes: int = GRID_CACHE_CAP_BYTES) -> list[str]:
        entries = []
        for d in self.grids.glob("*/*"):
            size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            entries.append((d.stat().st_mtime, size, d))
        total = sum(s for _, s, _ in entries)
        evicted, now = [], time.time()
        for mtime, size, d in sorted(entries):
            if total <= cap_bytes:
                break
            if now - mtime < GRID_PIN_S:
                continue
            shutil.rmtree(d, ignore_errors=True)
            total -= size
            evicted.append(d.parent.name)
        return evicted

    def clean_stale_jobs(self, older_than_s: float = 3600.0) -> None:
        for d in self.jobs.iterdir():
            if time.time() - d.stat().st_mtime > older_than_s:
                shutil.rmtree(d, ignore_errors=True)


def _symlink(target: Path, link: Path) -> None:
    if link.is_symlink() and os.readlink(link) == str(target):
        return
    tmp = link.with_name(f".{link.name}.{os.getpid()}")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    os.replace(tmp, link)


def connect(db_path: str | os.PathLike) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


# ------------------------------------------------------------------------------------- tier 0

@lru_cache(maxsize=1)
def core_descriptors() -> dict[str, dict]:
    if not CORE_CSV.exists():
        return {}
    keep = {"LCD": float, "PLD": float, "LFPD": float, "AV_VF": float, "ASA_m2_g": float,
            "Has_OMS": str, "Open_Metal_Sites": str, "All_Metals": str, "DISORDER": str}
    out = {}
    with open(CORE_CSV, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            d = {}
            for k, conv in keep.items():
                v = (row.get(k) or "").strip()
                try:
                    d[k] = conv(v) if v else None
                except ValueError:
                    d[k] = None
            out[row["filename"]] = d
    return out


def raspa_alias(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
        return name
    return "h" + hashlib.sha1(name.encode()).hexdigest()[:12]


def _perp_widths(lattice) -> tuple[float, float, float]:
    import numpy as np

    a, b, c = lattice.matrix
    v = abs(float(np.dot(a, np.cross(b, c))))
    return tuple(v / float(np.linalg.norm(np.cross(x, y))) for x, y in ((b, c), (c, a), (a, b)))


def _write_p1_cif(s, alias: str, path: Path) -> None:
    lat = s.lattice
    lines = [
        f"data_{alias}",
        f"_cell_length_a    {lat.a:.6f}", f"_cell_length_b    {lat.b:.6f}",
        f"_cell_length_c    {lat.c:.6f}", f"_cell_angle_alpha {lat.alpha:.6f}",
        f"_cell_angle_beta  {lat.beta:.6f}", f"_cell_angle_gamma {lat.gamma:.6f}",
        "_symmetry_space_group_name_H-M 'P 1'", "_symmetry_Int_Tables_number 1",
        "loop_", "_symmetry_equiv_pos_as_xyz", "'x,y,z'",
        "loop_", "_atom_site_label", "_atom_site_type_symbol",
        "_atom_site_fract_x", "_atom_site_fract_y", "_atom_site_fract_z",
    ]
    for site in s:
        el = site.specie.symbol
        x, y, z = (float(v) % 1.0 for v in site.frac_coords)
        lines.append(f"{el} {el} {x:.6f} {y:.6f} {z:.6f}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)


def describe(cif_path: str | os.PathLike, prepared_dir: str | os.PathLike) -> Descriptors:
    """Tier 0. Pure function of the CIF (plus the CoRE descriptor table); never raises."""
    name = Path(cif_path).stem
    alias = raspa_alias(name)
    try:
        import warnings

        from pymatgen.core import Structure

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = Structure.from_file(str(cif_path))
        if not s.is_ordered:
            raise ValueError("partial occupancies (disordered sites)")
        elements = sorted({site.specie.symbol for site in s})
        missing = [e for e in elements if e not in forcefield.uff_lj()]
        if missing:
            raise ValueError(f"no UFF parameters for {missing}")
        widths = _perp_widths(s.lattice)
        cells = tuple(max(1, math.ceil(2 * CUTOFF_A / w)) for w in widths)
        sphere_v = sum(4.0 / 3.0 * math.pi * float(site.specie.van_der_waals_radius or 1.7) ** 3
                       for site in s)
        prepared = Path(prepared_dir) / f"{alias}.cif"
        prepared.parent.mkdir(parents=True, exist_ok=True)
        _write_p1_cif(s, alias, prepared)
        return Descriptors(
            structure=name, parse_ok=True, alias=alias, prepared_path=str(prepared),
            n_atoms=len(s), n_atoms_sim=len(s) * cells[0] * cells[1] * cells[2],
            unit_cells=cells, density=float(s.density), volume=float(s.volume),
            perp_widths=tuple(round(w, 3) for w in widths),
            void_fraction_geo=max(0.0, 1.0 - sphere_v / s.volume),
            elements=elements, metals=[e for e in elements if e in METALS],
            core=core_descriptors().get(name, {}),
        )
    except Exception as e:  # an unusable structure is a normal tier-0 outcome
        return Descriptors(structure=name, parse_ok=False, alias=alias,
                           core=core_descriptors().get(name, {}),
                           error=f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------------------------- the oracle

class Oracle:
    """One per process. Safe to use from many processes against one DB (WAL + IMMEDIATE txns)."""

    def __init__(self, db_path: str | os.PathLike, run_id: str,
                 budget_cpu_s: float = DEFAULT_BUDGET_CPU_S,
                 labels_path: str | os.PathLike | None = PROJECT_ROOT / "data" / "labels.csv",
                 prepared_dir: str | os.PathLike = PROJECT_ROOT / "data" / "prepared",
                 env: RaspaEnv | None = None):
        self.db_path = str(db_path)
        self.run_id = run_id
        self.budget_cpu_s = float(budget_cpu_s)
        self.prepared_dir = Path(prepared_dir)
        self._labels_path = labels_path
        self.con = connect(self.db_path)
        self.env = env or RaspaEnv()

    # -- ledger ---------------------------------------------------------------------------

    def spent(self) -> float:
        """CPU-seconds debited: measured cost of finished calls + estimates of in-flight ones."""
        row = self.con.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='running' THEN est_cpu_s ELSE cpu_s END), 0) "
            "FROM evaluations WHERE run_id=?", (self.run_id,)).fetchone()
        return float(row[0])

    def remaining(self) -> float:
        return self.budget_cpu_s - self.spent()

    def _reserve(self, structure: str, tier: int, mode: str, est: float, timeout_s: float | None,
                 worker_id: str | None) -> str:
        eval_id = uuid.uuid4().hex
        self.con.execute("BEGIN IMMEDIATE")
        try:
            if self.remaining() < est:
                raise BudgetExhausted(
                    f"{structure} tier {tier}: needs ~{est:.1f}s, {self.remaining():.1f}s left")
            self.con.execute(
                "INSERT INTO evaluations (eval_id, run_id, structure, tier, mode, status, est_cpu_s, "
                "timeout_s, worker_id, started_at) VALUES (?,?,?,?,?, 'running', ?,?,?,?)",
                (eval_id, self.run_id, structure, tier, mode, est, timeout_s, worker_id,
                 time.time()))
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return eval_id

    def _finish(self, eval_id: str, status: str, cpu_s: float, wall_s: float,
                uptake=None, err=None, result: dict | None = None) -> None:
        self.con.execute(
            "UPDATE evaluations SET status=?, cpu_s=?, wall_s=?, uptake_molkg=?, uptake_err=?, "
            "result_json=?, finished_at=? WHERE eval_id=?",
            (status, cpu_s, wall_s, uptake, err, json.dumps(result or {}), time.time(), eval_id))

    # -- cost model -------------------------------------------------------------------------

    def fit_cost_model(self, run_only: bool = False) -> CostModel:
        """Refit cost coefficients (and the uptake prior) from finished phases in this DB."""
        prior = CostModel()
        where = "tier>0 AND status='ok'" + (" AND run_id=?" if run_only else "")
        args = (self.run_id,) if run_only else ()
        d, g, m, u = [], [], [], []
        for rj, up in self.con.execute(f"SELECT result_json, uptake_molkg FROM evaluations "
                                       f"WHERE {where}", args):
            u.append(up)
            for p in json.loads(rj or "{}").get("phases", []):
                if p.get("status") != "ok":
                    continue
                if p["kind"] == "mc_direct" and p.get("moves"):
                    d.append((p["cpu_s"] - prior.direct_a) / (p["moves"] * p["n_atoms_sim"]))
                elif p["kind"] == "makegrid":
                    g.append(p["cpu_s"] / (p["grid_points"] * p["n_atoms_sim"] * p["grid_types"]))
                elif p["kind"] == "mc_grid" and p.get("moves"):
                    m.append((p["cpu_s"] - prior.grid_mc_a) / p["moves"])
        med = lambda xs, default: sorted(xs)[len(xs) // 2] if len(xs) >= 3 else default  # noqa: E731
        return CostModel(direct_k=med(d, prior.direct_k), grid_g=med(g, prior.grid_g),
                         grid_mc_k=med(m, prior.grid_mc_k), uptake_prior=med(u, prior.uptake_prior),
                         n_fits=len(d) + len(g) + len(m))

    def framework_name(self, desc: Descriptors) -> str:
        """RASPA framework name, which keys the grid cache: scoped to this run, so one arm never
        gets a grid another arm paid for (resume keeps the run_id, so it keeps its grids)."""
        return f"{desc.alias}.{hashlib.sha1(self.run_id.encode()).hexdigest()[:8]}"

    @staticmethod
    def grid_points(desc: Descriptors) -> float:
        return desc.volume / GRID_SPACING_A ** 3

    def known_uptake(self, structure: str) -> float | None:
        """This structure's best existing GCMC estimate (any run in this DB), for cost planning."""
        row = self.con.execute(
            "SELECT uptake_molkg FROM evaluations WHERE structure=? AND status='ok' AND tier>0 "
            "ORDER BY tier DESC, finished_at DESC LIMIT 1", (structure,)).fetchone()
        return None if row is None else float(row[0])

    def plan(self, desc: Descriptors, tier: int, mode: str = "auto",
             model: CostModel | None = None, uptake_hint: float | None = None) -> Plan:
        m = model or CostModel()
        moves = m.moves(desc, tier, uptake_hint if uptake_hint is not None
                        else self.known_uptake(desc.structure))
        direct = m.direct_a + m.direct_k * moves * desc.n_atoms_sim
        cached = self.env.has_grid(self.framework_name(desc))
        build = 0.0 if cached else (m.grid_g * self.grid_points(desc) * desc.n_atoms_sim
                                    * len(forcefield.GRID_TYPES))
        grid = build + m.grid_mc_a + m.grid_mc_k * moves
        if mode == "auto":
            mode = "grid" if grid < direct else "direct"
        est = grid if mode == "grid" else direct
        return Plan(mode, mode == "grid" and not cached, est, direct, grid)

    # -- tier 0 -----------------------------------------------------------------------------

    def tier0(self, cif_path: str | os.PathLike, worker_id: str | None = None) -> Descriptors:
        name = Path(cif_path).stem
        eval_id = self._reserve(name, 0, "describe", 0.05, None, worker_id)
        c0, t0 = time.process_time(), time.time()
        d = describe(cif_path, self.prepared_dir)
        cpu, wall = time.process_time() - c0, time.time() - t0
        self._finish(eval_id, "ok" if d.parse_ok else "error", cpu, wall, result=asdict(d))
        return d

    # -- tier 1 / 2 -------------------------------------------------------------------------

    def _run_child(self, job: dict, cpu_limit_s: float, eval_id: str, cpu_before: float) -> dict:
        """Run one RASPA call in a child process under a kernel-enforced CPU-time limit.

        The limit is CPU time (RLIMIT_CPU -> SIGXCPU), not wall time: the budget is CPU-seconds,
        and a wall clock would kill jobs merely because the machine is busy. A wall backstop of
        WALL_BACKSTOP x the CPU limit catches a job that hangs without using CPU.
        """
        import psutil

        work = Path(tempfile.mkdtemp(dir=self.env.jobs, prefix="j-"))
        job = {**job, "workdir": str(work)}
        job_path, out_path, err_path = work / "job.json", work / "out.json", work / "stderr.txt"
        job_path.write_text(json.dumps(job))
        soft = max(1, math.ceil(cpu_limit_s))

        def limit() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (soft, soft + 5))

        t0 = time.time()
        backstop = t0 + WALL_BACKSTOP * soft + 30.0
        with open(err_path, "w") as err_f:
            proc = subprocess.Popen([sys.executable, "-m", "sieve._raspa_job", str(job_path),
                                     str(out_path)], stdout=subprocess.DEVNULL, stderr=err_f,
                                    cwd=work, start_new_session=True, preexec_fn=limit)
            ps = psutil.Process(proc.pid)
            wall_killed, beat = False, 0.0
            while True:
                pid, wstatus, ru = os.wait4(proc.pid, os.WNOHANG)
                if pid:
                    break
                now = time.time()
                if now - beat > HEARTBEAT_S:
                    beat = now
                    try:
                        c = ps.cpu_times()
                        used = cpu_before + c.user + c.system
                    except psutil.Error:
                        used = None
                    self.con.execute("UPDATE evaluations SET heartbeat_at=?, heartbeat_cpu_s=? "
                                     "WHERE eval_id=?", (now, used, eval_id))
                if now > backstop:
                    os.killpg(proc.pid, signal.SIGKILL)
                    _, wstatus, ru = os.wait4(proc.pid, 0)
                    wall_killed = True
                    break
                time.sleep(0.05)
        sig = os.WTERMSIG(wstatus) if os.WIFSIGNALED(wstatus) else None
        phase = {"kind": job["kind"], "cpu_s": ru.ru_utime + ru.ru_stime,
                 "wall_s": time.time() - t0, "exit_code": os.waitstatus_to_exitcode(wstatus),
                 "cpu_limit_s": soft}
        if wall_killed or sig in (signal.SIGXCPU, signal.SIGKILL):
            phase["status"] = "timeout"
            phase["killed_by"] = "wall_backstop" if wall_killed else "cpu_limit"
        elif phase["exit_code"] == 0 and out_path.exists():
            phase["status"] = "ok"
            phase["result"] = json.loads(out_path.read_text())
        else:
            phase["status"] = "error"
            phase["stderr"] = err_path.read_text(errors="replace")[-1500:]
        shutil.rmtree(work, ignore_errors=True)
        return phase

    def simulate(self, desc: Descriptors, tier: int, mode: str = "auto",
                 timeout_s: float | None = None, worker_id: str | None = None,
                 cost_model: CostModel | None = None,
                 uptake_hint: float | None = None) -> Evaluation:
        """Run one GCMC evaluation (grid build if needed + MC). Blocks.

        timeout_s is a CPU-second limit for the whole evaluation (all phases).
        Returns status 'ok', 'timeout' or 'error'; never raises for a failed simulation.
        Raises BudgetExhausted if the estimate does not fit in the remaining budget.
        """
        if not desc.parse_ok:
            raise ValueError(f"{desc.structure}: unusable structure ({desc.error})")
        t = TIERS[tier]
        model = cost_model or CostModel()
        if uptake_hint is None:
            uptake_hint = self.known_uptake(desc.structure)
        plan = self.plan(desc, tier, mode, model, uptake_hint)
        if timeout_s is None:
            timeout_s = min(t.max_timeout_s, max(30.0, TIMEOUT_FACTOR * plan.est_cpu_s))
        # never let one job spend more than what is left (concurrent jobs can still overrun
        # their estimates, so a run can end slightly over budget; the ledger reports it exactly)
        timeout_s = max(1.0, min(timeout_s, self.remaining()))
        eval_id = self._reserve(desc.structure, tier, plan.mode, plan.est_cpu_s, timeout_s,
                                worker_id)
        t0 = time.time()
        base = {
            "alias": self.framework_name(desc), "cif_path": desc.prepared_path,
            "elements": desc.elements,
            "unit_cells": list(desc.unit_cells), "forcefield": forcefield.NAME,
            "grid_types": list(forcefield.GRID_TYPES), "spacing": GRID_SPACING_A,
            "molecule": "CO2", "temperature": 298.0, "pressure": 100000.0,
            # Only affects the excess number; ranking uses absolute loading.
            "helium_void_fraction": 0.75,
        }
        cost = {"n_atoms_sim": desc.n_atoms_sim, "cycles": t.cycles + t.init_cycles,
                "grid_points": self.grid_points(desc), "grid_types": len(forcefield.GRID_TYPES)}
        phases: list[dict] = []
        used = lambda: sum(p["cpu_s"] for p in phases)  # noqa: E731
        if plan.mode == "grid":
            self.env.touch_grid(self.framework_name(desc))
            if plan.build_grid:
                private = self.env.private_root()
                try:
                    p = self._run_child({**base, "kind": "makegrid", "raspa_root": str(private)},
                                        timeout_s, eval_id, 0.0)
                    if p["status"] == "ok" and not (p["result"].get("grid_ok")
                                                    and self.env.publish(private,
                                                                        self.framework_name(desc))):
                        p["status"], p["stderr"] = "error", "grid build did not complete"
                finally:
                    shutil.rmtree(private, ignore_errors=True)
                phases.append({**p, **cost})
                self.env.evict()
        if not phases or phases[-1]["status"] == "ok":
            kind = "mc_grid" if plan.mode == "grid" else "mc_direct"
            p = self._run_child({**base, "kind": kind, "raspa_root": str(self.env.root),
                                 "cycles": t.cycles, "init_cycles": t.init_cycles},
                                max(1.0, timeout_s - used()), eval_id, used())
            if p["status"] == "ok":
                n = p["result"]["molecules_per_cell"] * math.prod(desc.unit_cells)
                p["moves"] = (t.cycles + t.init_cycles) * max(MIN_STEPS_PER_CYCLE, n)
            phases.append({**p, **cost})

        cpu, wall = used(), time.time() - t0
        last = phases[-1]
        meta = {"plan": asdict(plan), "phases": phases, "unit_cells": list(desc.unit_cells),
                "n_atoms_sim": desc.n_atoms_sim, "uptake_hint": uptake_hint}
        status = last["status"]
        if status == "timeout":
            self.con.execute("INSERT INTO timeouts VALUES (?,?,?,?,?,?,?,?)",
                             (eval_id, self.run_id, desc.structure, tier, last["kind"], timeout_s,
                              desc.n_atoms_sim, time.time()))
        if status != "ok":
            self._finish(eval_id, status, cpu, wall, result=meta)
            return Evaluation(eval_id, desc.structure, tier, plan.mode, status, None, None, cpu,
                              wall, timeout_s, plan.est_cpu_s, meta)
        r = last["result"]
        meta.update({k: r[k] for k in ("excess_molkg", "excess_err", "molecules_per_cell")})
        self._finish(eval_id, "ok", cpu, wall, r["uptake_molkg"], r["uptake_err"], meta)
        return Evaluation(eval_id, desc.structure, tier, plan.mode, "ok", r["uptake_molkg"],
                          r["uptake_err"], cpu, wall, timeout_s, plan.est_cpu_s, meta)

    # -- held-out labels: post-hoc scoring only -------------------------------------------------

    def score_ranking(self, ranking: list[str], k: int = 10,
                      name_col: str = "name", value_col: str = "co2_uptake") -> dict:
        """Compare a final ranking to held-out labels. Never call this from agent code.

        Returns aggregate metrics only (no per-structure label values) so nothing it emits
        can be fed back into a model prompt.
        """
        if not self._labels_path or not Path(self._labels_path).exists():
            return {"available": False}
        labels: dict[str, float] = {}
        with open(self._labels_path) as f:
            for row in csv.DictReader(f):
                try:
                    labels[row[name_col]] = float(row[value_col])
                except (KeyError, ValueError):
                    continue
        scored = [s for s in ranking if s in labels]
        if not scored:
            return {"available": True, "overlap": 0}
        true_top = set(sorted(labels, key=labels.get, reverse=True)[:k])
        best = max(labels.values())
        return {
            "available": True,
            "overlap": len(scored),
            f"precision_at_{k}": len(set(scored[:k]) & true_top) / k,
            "best_found_over_best_label": max(labels[s] for s in scored[:k]) / best,
            "best_label_value": best,
        }


# ------------------------------------------------------------------------------------- smoke

def _smoke() -> None:
    with tempfile.TemporaryDirectory() as d:
        o = Oracle(Path(d) / "smoke.db", "smoke", labels_path=None, prepared_dir=Path(d))
        desc = o.tier0(bundled_cif_dir() / "IRMOF-1.cif")
        for mode in ("direct", "grid"):
            ev = o.simulate(desc, 1, mode=mode)
            kinds = "+".join(f"{p['kind']}:{p['cpu_s']:.1f}s" for p in ev.result["phases"])
            print(f"{ev.structure} tier-1 [{mode}] CO2 @ 298 K, 1 bar: {ev.uptake_molkg:.3f} +/- "
                  f"{ev.uptake_err:.3f} mol/kg (abs, NOT converged)  cpu={ev.cpu_s:.1f}s "
                  f"({kinds})  est={ev.est_cpu_s:.1f}s")
        print(f"ledger={o.spent():.1f}s  cells={desc.unit_cells}")


if __name__ == "__main__":
    if sys.argv[1:] == ["smoke"]:
        _smoke()
    else:
        print("usage: python -m sieve.oracle smoke")

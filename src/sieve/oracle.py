"""The oracle: the only module that runs simulations or reads labels.csv.

Three fidelity tiers behind one interface:
  tier 0  pymatgen CIF parse -> geometric descriptors (proxy only)
  tier 1  RASPA2 GCMC, 500 cycles + 100 init   (nominal +/- 18%, NOT converged)
  tier 2  RASPA2 GCMC, 2000 cycles + 400 init  (nominal +/- 8.6%)

Every call is logged to SQLite (`evaluations`, plus `timeouts` on a kill). The budget ledger
is *derived* from that log -- spent = sum of measured child CPU-seconds -- so there is no
separate counter that can drift from the record after a crash.

Uptake reported is ABSOLUTE loading in mol/kg at 298 K / 1 bar. Absolute loading does not
depend on the helium void fraction; excess loading does, and is recorded but not ranked on.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

CUTOFF_A = 12.8  # RASPA2.create_script hardcodes CutOff 12.8
DEFAULT_BUDGET_CPU_S = 4 * 3600.0


@dataclass(frozen=True)
class Tier:
    id: int
    cycles: int
    init_cycles: int
    nominal_rel_err: float
    max_timeout_s: float


TIERS = {
    1: Tier(1, cycles=500, init_cycles=100, nominal_rel_err=0.18, max_timeout_s=180.0),
    2: Tier(2, cycles=2000, init_cycles=400, nominal_rel_err=0.086, max_timeout_s=720.0),
}

# Cost prior, CPU-s ~= a + b * (init+cycles) * framework_atoms_in_supercell.
# Calibrated on IRMOF-1 tier 1 (424 atoms, 1x1x1, 600 cycles -> 12.6 s on Apple M-series).
# The orchestrator refines this from the evaluations log via fit_cost_model().
COST_PRIOR_A = 0.6
COST_PRIOR_B = 4.7e-5

METALS = {
    "Li", "Be", "Na", "Mg", "Al", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu",
    "Zn", "Ga", "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Cs",
    "Ba", "La", "Ce", "Pr", "Nd", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu", "Hf",
    "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Pb", "Bi", "U",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    eval_id      TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    structure    TEXT NOT NULL,
    tier         INTEGER NOT NULL,
    status       TEXT NOT NULL,          -- running | ok | timeout | error
    est_cpu_s    REAL,
    timeout_s    REAL,
    cpu_s        REAL,                   -- measured user+sys of the job process; debits budget
    wall_s       REAL,
    uptake_molkg REAL,                   -- absolute loading, mol/kg
    uptake_err   REAL,                   -- RASPA block-average error on the above
    result_json  TEXT,                   -- full result / descriptors / error text
    worker_id    TEXT,
    started_at   REAL NOT NULL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS ix_eval_run ON evaluations(run_id, status);
CREATE TABLE IF NOT EXISTS timeouts (
    eval_id      TEXT PRIMARY KEY REFERENCES evaluations(eval_id),
    run_id       TEXT NOT NULL,
    structure    TEXT NOT NULL,
    tier         INTEGER NOT NULL,
    timeout_s    REAL NOT NULL,
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
    n_atoms: int = 0
    n_atoms_sim: int = 0  # atoms in the supercell actually simulated
    unit_cells: tuple[int, int, int] = (1, 1, 1)
    density: float = 0.0  # g/cm3
    volume: float = 0.0  # A3, unit cell
    perp_widths: tuple[float, float, float] = (0.0, 0.0, 0.0)
    void_fraction_geo: float = 0.0  # crude: 1 - sum(vdW sphere volumes)/V, overlaps ignored
    elements: list[str] = field(default_factory=list)
    metals: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Evaluation:
    eval_id: str
    structure: str
    tier: int
    status: str  # ok | timeout | error
    uptake_molkg: float | None
    uptake_err: float | None
    cpu_s: float
    wall_s: float
    timeout_s: float | None
    est_cpu_s: float | None
    result: dict

    @property
    def nominal_rel_err(self) -> float:
        return TIERS[self.tier].nominal_rel_err if self.tier in TIERS else float("nan")


# ---------------------------------------------------------------------------------------- env

def raspa_package_dir() -> Path:
    import RASPA2

    return Path(RASPA2.__file__).resolve().parent


def bundled_cif_dir() -> Path:
    return raspa_package_dir() / "share/raspa/structures/cif"


def raspa_dir() -> str:
    """A short path to the RASPA2 share tree.

    libraspa builds file paths into fixed char[256] buffers; a long venv path (this machine's
    is ~230 chars) overflows them and macOS FORTIFY aborts with SIGTRAP. A symlink under
    ~/.cache keeps it short.
    """
    link = Path.home() / ".cache/sieve/raspa"
    target = raspa_package_dir()
    if not (link.is_symlink() and link.resolve() == target):
        link.parent.mkdir(parents=True, exist_ok=True)
        tmp = link.with_name(f"raspa.{os.getpid()}")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(target)
        os.replace(tmp, link)
    return str(link)


def connect(db_path: str | os.PathLike) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


# ------------------------------------------------------------------------------------- tier 0

def _perp_widths(lattice) -> tuple[float, float, float]:
    import numpy as np

    a, b, c = lattice.matrix
    v = abs(float(np.dot(a, np.cross(b, c))))
    return tuple(v / float(np.linalg.norm(np.cross(x, y))) for x, y in ((b, c), (c, a), (a, b)))


def describe(cif_path: str | os.PathLike) -> Descriptors:
    """Tier 0. Pure function of the CIF; never raises on a bad file."""
    name = Path(cif_path).stem
    try:
        import warnings

        from pymatgen.core import Structure

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = Structure.from_file(str(cif_path))
        widths = _perp_widths(s.lattice)
        cells = tuple(max(1, math.ceil(2 * CUTOFF_A / w)) for w in widths)
        sphere_v = 0.0
        for site in s:
            r = site.specie.van_der_waals_radius or 1.7
            sphere_v += 4.0 / 3.0 * math.pi * float(r) ** 3
        elements = sorted({el.symbol for el in s.composition.elements})
        return Descriptors(
            structure=name,
            parse_ok=True,
            n_atoms=len(s),
            n_atoms_sim=len(s) * cells[0] * cells[1] * cells[2],
            unit_cells=cells,
            density=float(s.density),
            volume=float(s.volume),
            perp_widths=tuple(round(w, 3) for w in widths),
            void_fraction_geo=max(0.0, 1.0 - sphere_v / s.volume),
            elements=elements,
            metals=[e for e in elements if e in METALS],
        )
    except Exception as e:  # unparseable structure is a normal tier-0 outcome
        return Descriptors(structure=name, parse_ok=False, error=f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------------------------- the oracle

class Oracle:
    """One per process. Safe to use from many processes against one DB (WAL + IMMEDIATE txns)."""

    def __init__(self, db_path: str | os.PathLike, run_id: str,
                 budget_cpu_s: float = DEFAULT_BUDGET_CPU_S,
                 labels_path: str | os.PathLike | None = "data/labels.csv"):
        self.db_path = str(db_path)
        self.run_id = run_id
        self.budget_cpu_s = float(budget_cpu_s)
        self._labels_path = labels_path
        self.con = connect(self.db_path)
        self._raspa_dir = raspa_dir()

    # -- ledger ---------------------------------------------------------------------------

    def spent(self) -> float:
        """CPU-seconds debited: measured cost of finished calls + estimates of in-flight ones."""
        row = self.con.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='running' THEN est_cpu_s ELSE cpu_s END), 0) "
            "FROM evaluations WHERE run_id=?", (self.run_id,)).fetchone()
        return float(row[0])

    def remaining(self) -> float:
        return self.budget_cpu_s - self.spent()

    def _reserve(self, structure: str, tier: int, est: float, timeout_s: float | None,
                 worker_id: str | None, enforce: bool) -> str:
        eval_id = uuid.uuid4().hex
        self.con.execute("BEGIN IMMEDIATE")
        try:
            if enforce and self.remaining() < est:
                raise BudgetExhausted(
                    f"{structure} tier {tier}: needs ~{est:.1f}s, {self.remaining():.1f}s left")
            self.con.execute(
                "INSERT INTO evaluations (eval_id, run_id, structure, tier, status, est_cpu_s, "
                "timeout_s, worker_id, started_at) VALUES (?,?,?,?, 'running', ?,?,?,?)",
                (eval_id, self.run_id, structure, tier, est, timeout_s, worker_id, time.time()))
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

    def fit_cost_model(self) -> tuple[float, float]:
        """Refit b from this DB's completed GCMC calls (median of per-call estimates)."""
        rows = self.con.execute(
            "SELECT tier, cpu_s, json_extract(result_json, '$.n_atoms_sim') AS n "
            "FROM evaluations WHERE status='ok' AND tier>0 AND n IS NOT NULL").fetchall()
        bs = sorted((r["cpu_s"] - COST_PRIOR_A) / ((TIERS[r["tier"]].cycles
                     + TIERS[r["tier"]].init_cycles) * r["n"]) for r in rows if r["n"])
        return (COST_PRIOR_A, bs[len(bs) // 2]) if len(bs) >= 3 else (COST_PRIOR_A, COST_PRIOR_B)

    def estimate_cost(self, desc: Descriptors, tier: int, model: tuple[float, float] | None = None
                      ) -> float:
        if tier == 0:
            return 0.25
        a, b = model or (COST_PRIOR_A, COST_PRIOR_B)
        t = TIERS[tier]
        return a + b * (t.cycles + t.init_cycles) * max(desc.n_atoms_sim, 1)

    # -- tier 0 -----------------------------------------------------------------------------

    def tier0(self, cif_path: str | os.PathLike, worker_id: str | None = None) -> Descriptors:
        name = Path(cif_path).stem
        eval_id = self._reserve(name, 0, 0.25, None, worker_id, enforce=True)
        c0, t0 = time.process_time(), time.time()
        d = describe(cif_path)
        cpu, wall = time.process_time() - c0, time.time() - t0
        self._finish(eval_id, "ok" if d.parse_ok else "error", cpu, wall, result=asdict(d))
        return d

    # -- tier 1 / 2 -------------------------------------------------------------------------

    def simulate(self, cif_path: str | os.PathLike, tier: int, desc: Descriptors | None = None,
                 timeout_s: float | None = None, worker_id: str | None = None,
                 cost_model: tuple[float, float] | None = None) -> Evaluation:
        """Run one GCMC job in a child process. Blocks. A timeout returns status='timeout'."""
        t = TIERS[tier]
        cif_path = str(Path(cif_path).resolve())
        desc = desc or describe(cif_path)
        if not desc.parse_ok:
            raise ValueError(f"{desc.structure}: unparseable CIF, run tier 0 first ({desc.error})")
        est = self.estimate_cost(desc, tier, cost_model)
        if timeout_s is None:
            timeout_s = min(t.max_timeout_s, max(30.0, 4.0 * est))
        eval_id = self._reserve(desc.structure, tier, est, timeout_s, worker_id, enforce=True)

        job = {
            "cif_path": cif_path, "molecule": "CO2", "temperature": 298.0, "pressure": 100000.0,
            # Brief specifies a fixed 0.75. It only affects the excess number, not the absolute
            # loading we rank on. Replace with a per-structure helium run if excess is needed.
            "helium_void_fraction": 0.75,
            "unit_cells": list(desc.unit_cells), "cycles": t.cycles, "init_cycles": t.init_cycles,
            "forcefield": "CrystalGenerator", "raspa_dir": self._raspa_dir,
        }
        with tempfile.TemporaryDirectory(prefix="sieve-") as tmp:
            job_path, out_path, err_path = (os.path.join(tmp, n) for n in
                                            ("job.json", "out.json", "stderr.txt"))
            json.dump(job, open(job_path, "w"))
            t0 = time.time()
            with open(err_path, "w") as err_f:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "sieve._raspa_job", job_path, out_path],
                    stdout=subprocess.DEVNULL, stderr=err_f, cwd=tmp, start_new_session=True)
                status, ru = None, None
                while True:
                    pid, wstatus, ru = os.wait4(proc.pid, os.WNOHANG)
                    if pid:
                        break
                    if time.time() - t0 > timeout_s:
                        os.killpg(proc.pid, 9)
                        _, wstatus, ru = os.wait4(proc.pid, 0)
                        status = "timeout"
                        break
                    time.sleep(0.05)
                proc.returncode = os.waitstatus_to_exitcode(wstatus)
            wall = time.time() - t0
            cpu = ru.ru_utime + ru.ru_stime

            meta = {"n_atoms_sim": desc.n_atoms_sim, "unit_cells": list(desc.unit_cells),
                    "exit_code": proc.returncode}
            if status == "timeout":
                self._finish(eval_id, "timeout", cpu, wall, result=meta)
                self.con.execute(
                    "INSERT INTO timeouts VALUES (?,?,?,?,?,?,?)",
                    (eval_id, self.run_id, desc.structure, tier, timeout_s, desc.n_atoms_sim,
                     time.time()))
                return Evaluation(eval_id, desc.structure, tier, "timeout", None, None, cpu, wall,
                                  timeout_s, est, meta)
            if proc.returncode != 0 or not os.path.exists(out_path):
                meta["stderr"] = open(err_path).read()[-2000:]
                self._finish(eval_id, "error", cpu, wall, result=meta)
                return Evaluation(eval_id, desc.structure, tier, "error", None, None, cpu, wall,
                                  timeout_s, est, meta)
            res = {**json.load(open(out_path)), **meta}
        self._finish(eval_id, "ok", cpu, wall, res["uptake_molkg"], res["uptake_err"], res)
        return Evaluation(eval_id, desc.structure, tier, "ok", res["uptake_molkg"],
                          res["uptake_err"], cpu, wall, timeout_s, est, res)

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
    import tempfile as tf

    cif = bundled_cif_dir() / "IRMOF-1.cif"
    with tf.TemporaryDirectory() as d:
        o = Oracle(Path(d) / "smoke.db", "smoke", labels_path=None)
        desc = o.tier0(cif)
        ev = o.simulate(cif, 1, desc)
        print(f"{ev.structure} tier-1 CO2 @ 298 K, 1 bar: {ev.uptake_molkg:.3f} +/- "
              f"{ev.uptake_err:.3f} mol/kg (abs, NOT converged)  cpu={ev.cpu_s:.1f}s  "
              f"est={ev.est_cpu_s:.1f}s  cells={desc.unit_cells}  ledger={o.spent():.1f}s")


if __name__ == "__main__":
    if sys.argv[1:] == ["smoke"]:
        _smoke()
    else:
        print("usage: python -m sieve.oracle smoke")

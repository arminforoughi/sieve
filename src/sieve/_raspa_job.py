"""Child-process entry point for one RASPA call. Only oracle.py launches this.

Usage: python -m sieve._raspa_job <job.json> <result.json>

One RASPA call per process: libraspa keeps global state between calls and leaks memory, and a
separate process is what the oracle SIGKILLs on timeout. RASPA runs in file mode (input file and
CIF in the job's cwd, results under ./Output) because its grid code refuses to stream.
"""

import json
import os
import re
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)  # RASPA2's output_parser regexes

from ctypes import c_bool, c_char_p, c_void_p, cdll  # noqa: E402

import RASPA2.raspa2 as R  # noqa: E402

INTERACTION = re.compile(r"^\s*(\S+)\s+-\s+(C_co2|O_co2)\s+\[(\w+)\]\s+p_0/k_B:\s*([-\d.eE+]+)", re.M)


def _orphan_watchdog() -> None:
    # ctypes releases the GIL during lib.run, so this thread keeps running. If the oracle dies
    # (including kill -9) we are re-parented; exit instead of burning a core nobody accounts for.
    parent = os.getppid()
    while True:
        time.sleep(1.0)
        if os.getppid() != parent:
            os._exit(3)


def _script(job: dict) -> str:
    cells = " ".join(str(c) for c in job["unit_cells"])
    head = [
        f"Forcefield                    {job['forcefield']}",
        "CutOff                        12.8",
        "ChargeMethod                  None",
        "UseChargesFromCIFFile         no",
        "Framework                     0",
        f"FrameworkName                 {job['alias']}",
        "InputFileType                 cif",
        "RemoveAtomNumberCodeFromLabel yes",
        f"UnitCells                     {cells}",
    ]
    grid = [
        f"NumberOfGrids                 {len(job['grid_types'])}",
        f"GridTypes                     {' '.join(job['grid_types'])}",
        f"SpacingVDWGrid                {job['spacing']}",
        f"SpacingCoulombGrid            {job['spacing']}",
    ]
    comp = [
        f"Component 0 MoleculeName             {job['molecule']}",
        "            MoleculeDefinition       TraPPE",
        "            TranslationProbability   1.0",
        "            RotationProbability      1.0",
        "            ReinsertionProbability   1.0",
        "            SwapProbability          1.0",
        "            CreateNumberOfMolecules  0",
    ]
    if job["kind"] == "makegrid":
        return "\n".join(["SimulationType                MakeGrid", *head, *grid, *comp]) + "\n"
    mc = [
        "SimulationType                MonteCarlo",
        f"NumberOfCycles                {job['cycles']}",
        f"NumberOfInitializationCycles  {job['init_cycles']}",
        f"PrintEvery                    {max(job['cycles'], 1)}",
        "RestartFile                   no",
        "Movies                        no",
        *head,
        f"HeliumVoidFraction            {job['helium_void_fraction']}",
        f"ExternalTemperature           {job['temperature']}",
        f"ExternalPressure              {job['pressure']}",
    ]
    if job["kind"] == "mc_grid":
        mc += ["UseTabularGrid                yes", *grid]
    return "\n".join([*mc, *comp]) + "\n"


def _check_interactions(raw: str, job: dict) -> dict:
    """Every framework element must interact with CO2 through a real LJ potential."""
    seen = {}
    for a, b, kind, eps in INTERACTION.findall(raw):
        seen.setdefault(a, []).append((b, kind, float(eps)))
    bad = []
    for el in job["elements"]:
        rows = seen.get(el)
        if not rows or any(not k.startswith("LENNARD_JONES") or e <= 0.0 for _, k, e in rows):
            bad.append({el: rows})
    if bad:
        raise RuntimeError(f"framework atoms without a CO2 LJ interaction: {bad}")
    return {el: seen[el] for el in job["elements"]}


def main() -> None:
    threading.Thread(target=_orphan_watchdog, daemon=True).start()
    job_path, out_path = sys.argv[1], sys.argv[2]
    job = json.load(open(job_path))
    os.chdir(job["workdir"])
    with open(f"{job['alias']}.cif", "w") as f:
        f.write(open(job["cif_path"]).read())
    with open("simulation.input", "w") as f:
        f.write(_script(job))

    lib = cdll.LoadLibrary(os.path.join(R.libraspa_dir, R.libraspa_file))
    lib.run.argtypes = (c_char_p, c_char_p, c_char_p, c_bool)
    lib.run.restype = c_void_p
    lib.run(b"simulation.input", b"", job["raspa_root"].encode("ascii"), False)

    out_dir = os.path.join("Output", "System_0")
    raw = open(os.path.join(out_dir, os.listdir(out_dir)[0])).read()
    interactions = _check_interactions(raw, job)
    result = {"kind": job["kind"]}
    if job["kind"] == "makegrid":
        result["grid_ok"] = "Percentage finished                      : 100" in raw
    else:
        from RASPA2.output_parser import parse

        mol = parse(raw)["Number of molecules"][job["molecule"]]
        abs_ = mol["Average loading absolute [mol/kg framework]"]
        exc = mol["Average loading excess [mol/kg framework]"]
        n = mol["Average loading absolute [molecules/unit cell]"]
        result.update(uptake_molkg=float(abs_[0]), uptake_err=float(abs_[2]),
                      excess_molkg=float(exc[0]), excess_err=float(exc[2]),
                      molecules_per_cell=float(n[0]))
    result["lj_check"] = {el: [round(e, 3) for _, _, e in rows] for el, rows in interactions.items()}
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f)
    os.replace(tmp, out_path)


if __name__ == "__main__":
    main()

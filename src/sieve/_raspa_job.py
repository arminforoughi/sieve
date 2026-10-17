"""Child-process entry point for one RASPA call. Only oracle.py launches this.

Usage: python -m sieve._raspa_job <job.json> <result.json>

Job kinds: makegrid, mc_direct, mc_grid, movie (one CO2 component, the screen's kinds) and, for
the science view's capture properties, mc_mixture (multi-component GCMC with identity changes)
and widom (infinite-dilution Widom insertion per component). A job with "enthalpy": true also
returns RASPA's fluctuation enthalpy of adsorption.

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

INTERACTION = re.compile(r"^\s*(\S+)\s+-\s+(C_co2|O_co2|N_n2)\s+\[(\w+)\]\s+p_0/k_B:\s*([-\d.eE+]+)", re.M)


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
    if job.get("components"):
        comp = _components(job)
    else:
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
    if job["kind"] == "movie":  # visualization only: snapshots of where CO2 sits
        mc = [x.replace("Movies                        no", "Movies                        yes") for x in mc]
        mc += [f"WriteMoviesEvery              {job.get('movie_every', 20)}"]
    return "\n".join([*mc, *comp]) + "\n"


def _components(job: dict) -> list[str]:
    """Component blocks for mc_mixture / widom jobs: job["components"] = [{"molecule", "mol_fraction"}]."""
    comps = job["components"]
    names = [c["molecule"] for c in comps]
    out = []
    for i, c in enumerate(comps):
        out += [f"Component {i} MoleculeName             {c['molecule']}",
                "            MoleculeDefinition       TraPPE"]
        if job["kind"] == "widom":
            out += ["            WidomProbability         1.0"]
        else:
            out += [f"            MolFraction              {c['mol_fraction']}",
                    "            TranslationProbability   1.0",
                    "            ReinsertionProbability   1.0",
                    "            SwapProbability          2.0"]
            out += ["            RotationProbability      1.0"]
            if len(comps) > 1:
                out += ["            IdentityChangeProbability 1.0",
                        f"            NumberOfIdentityChanges  {len(comps)}",
                        f"            IdentityChangesList      {' '.join(str(j) for j in range(len(names)))}"]
        out += ["            CreateNumberOfMolecules  0"]
    return out


_NUM = r"([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)"


def parse_enthalpy(raw: str) -> dict[str, dict]:
    """RASPA's fluctuation-formula enthalpy of adsorption in kJ/mol (negative = exothermic).

    The "Enthalpy of adsorption:" section holds one sub-block per component in a mixture
    ("Enthalpy of adsorption component 0 [CO2]") plus a total; a single-component run has only
    "Total enthalpy of adsorption". Keys: the component name, or "_total"."""
    start = raw.find("Enthalpy of adsorption:\n")
    if start < 0:
        return {}
    end = raw.find("derivative of the chemical potential", start)
    sec = raw[start:end if end > 0 else len(raw)]
    out = {}
    heads = list(re.finditer(r"^\s*(Enthalpy of adsorption component \d+ \[(\S+)\]|Total enthalpy of adsorption.*)$",
                             sec, re.M))
    for k, h in enumerate(heads):
        body = sec[h.end():heads[k + 1].start() if k + 1 < len(heads) else len(sec)]
        mm = re.search(r"Average\s+" + _NUM + r"\s+\+/-\s+" + _NUM + r"\s+\[K\]\s*\n\s*" + _NUM
                       + r"\s+\+/-\s+" + _NUM + r"\s+\[KJ/MOL\]", body)
        if mm:
            out[h.group(2) or "_total"] = {"kjmol": float(mm.group(3)), "err": float(mm.group(4))}
    return out


def parse_widom(raw: str) -> dict[str, dict]:
    """Per component: Henry coefficient [mol/kg/Pa] and <U_gh>_1-<U_h>_0 [kJ/mol] from Widom insertion.
    RASPA's note: the heat of adsorption is dH = <U_gh>_1-<U_h>_0 - <U_g> - RT (<U_g> = 0, rigid)."""
    out: dict[str, dict] = {}
    for name, v, e in re.findall(r"\[(\S+)\] Average Henry coefficient:\s+" + _NUM + r"\s+\+/-\s+" + _NUM
                                 + r"\s+\[mol/kg/Pa\]", raw):
        out.setdefault(name, {}).update(kh_molkgpa=float(v), kh_err=float(e))
    for name, v, e in re.findall(r"\[(\S+)\] Average\s+<U_gh>_1-<U_h>_0:.*?\(\s*" + _NUM + r"\s+\+/-\s+"
                                 + _NUM + r"\s+kJ/mol\)", raw):
        out.setdefault(name, {}).update(u_kjmol=float(v), u_err=float(e))
    return out


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
    if job.get("save_output"):  # science/debug: keep RASPA's raw output (the workdir is deleted)
        with open(job["save_output"], "w") as f:
            f.write(raw)
    interactions = _check_interactions(raw, job)
    result = {"kind": job["kind"]}
    if job["kind"] == "makegrid":
        result["grid_ok"] = "Percentage finished                      : 100" in raw
    else:
        from RASPA2.output_parser import parse

        if job["kind"] == "movie":
            mdir = os.path.join("Movies", "System_0")
            result["movie"] = {f: open(os.path.join(mdir, f)).read() for f in os.listdir(mdir)
                               if "component_CO2" in f or "allcomponents" in f}
        if job["kind"] == "widom":
            result["widom"] = parse_widom(raw)
            result["lj_check"] = {el: [round(e, 3) for _, _, e in rows] for el, rows in interactions.items()}
            _dump(result, out_path)
            return
        if job.get("enthalpy") or job["kind"] == "mc_mixture":
            result["enthalpy"] = parse_enthalpy(raw)
        if job["kind"] == "mc_mixture":
            parsed = parse(raw)["Number of molecules"]
            result["loadings"] = {}
            for c in job["components"]:
                mol = parsed[c["molecule"]]
                abs_ = mol["Average loading absolute [mol/kg framework]"]
                result["loadings"][c["molecule"]] = {
                    "molkg": float(abs_[0]), "err": float(abs_[2]),
                    "molecules_per_cell": float(mol["Average loading absolute [molecules/unit cell]"][0])}
            result["lj_check"] = {el: [round(e, 3) for _, _, e in rows] for el, rows in interactions.items()}
            _dump(result, out_path)
            return
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


def _dump(result: dict, out_path: str) -> None:
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f)
    os.replace(tmp, out_path)


if __name__ == "__main__":
    main()

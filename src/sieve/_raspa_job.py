"""Child-process entry point for one GCMC job. Only oracle.py launches this.

Usage: python -m sieve._raspa_job <job.json> <result.json>

Runs in its own process so a timeout can SIGKILL it without touching the parent.
RASPA prints framework diagnostics to stdout, so the result goes to a file, not stdout.
"""

import json
import os
import sys
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)  # RASPA2's output_parser regexes

from ctypes import c_bool, c_char_p, c_void_p, cast, cdll  # noqa: E402

import RASPA2.raspa2 as R  # noqa: E402
from RASPA2.output_parser import parse  # noqa: E402


def _pair(entry):
    # RASPA2 parser yields [value, "+/-", error, "[-]"]
    return float(entry[0]), float(entry[2])


def main() -> None:
    job_path, out_path = sys.argv[1], sys.argv[2]
    job = json.load(open(job_path))
    cif_text = open(job["cif_path"]).read()

    # Same input RASPA2.run(cif_text, "CO2", ...) builds; we call libraspa directly so this
    # process *is* the simulation (RASPA2.run forks a grandchild that a kill would orphan).
    script = R.create_script(
        job["molecule"],
        temperature=job["temperature"],
        pressure=job["pressure"],
        helium_void_fraction=job["helium_void_fraction"],
        unit_cells=tuple(job["unit_cells"]),
        cycles=job["cycles"],
        init_cycles=job["init_cycles"],
        forcefield=job["forcefield"],
        input_file_type="cif",
    )
    lib = cdll.LoadLibrary(os.path.join(R.libraspa_dir, R.libraspa_file))
    lib.run.argtypes = (c_char_p, c_char_p, c_char_p, c_bool)
    lib.run.restype = c_void_p
    ptr = lib.run(script.encode("ascii"), cif_text.encode("ascii", "replace"),
                  job["raspa_dir"].encode("ascii"), True)
    raw = cast(ptr, c_char_p).value.decode("utf-8", "replace")

    parsed = parse(raw)
    mol = parsed["Number of molecules"][job["molecule"]]
    abs_v, abs_e = _pair(mol["Average loading absolute [mol/kg framework]"])
    exc_v, exc_e = _pair(mol["Average loading excess [mol/kg framework]"])
    n_charges = parsed.get("Framework Status", {}).get("Number of charges", [None])[0]
    result = {
        "uptake_molkg": abs_v,
        "uptake_err": abs_e,
        "excess_molkg": exc_v,
        "excess_err": exc_e,
        "framework_charges": n_charges,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f)
    os.replace(tmp, out_path)


if __name__ == "__main__":
    main()

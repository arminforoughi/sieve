"""Benchmark direct vs grid GCMC on a size-stratified sample of accessible CoRE structures.

For each structure: tier 1 + tier 2 in grid mode (grid built once, reused) and in direct mode.
Everything goes through the oracle, so all phases land in the DB for fit_cost_model().
Usage: uv run python scripts/bench_modes.py <db> [n_structures] [n_procs]
"""
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

from sieve.oracle import TIERS, Descriptors, Oracle, PROJECT_ROOT

DB = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 12
PROCS = int(sys.argv[3]) if len(sys.argv) > 3 else 7


def load_sample() -> list[dict]:
    pool = [json.loads(l) for l in open(PROJECT_ROOT / "data" / "pool.jsonl")]
    acc = [d for d in pool if d["parse_ok"] and (d["core"].get("PLD") or 0) >= 3.3]
    acc.sort(key=lambda d: d["n_atoms_sim"])
    qs = [0.03 + 0.92 * i / (N - 1) for i in range(N)]
    return [acc[int(q * (len(acc) - 1))] for q in qs]


def run(d: dict) -> list[str]:
    d = {k: v for k, v in d.items() if k != "tier0_cpu_s"}
    d["unit_cells"] = tuple(d["unit_cells"]); d["perp_widths"] = tuple(d["perp_widths"])
    desc = Descriptors(**d)
    o = Oracle(DB, "bench-modes", budget_cpu_s=1e9, labels_path=None)
    out = []
    for mode, tier in (("grid", 1), ("grid", 2), ("direct", 1), ("direct", 2)):
        ev = o.simulate(desc, tier, mode=mode, worker_id=f"bench-{desc.alias}",
                        timeout_s=TIERS[tier].max_timeout_s)
        u = f"{ev.uptake_molkg:.3f}+/-{ev.uptake_err:.3f}" if ev.uptake_molkg is not None else ev.status
        out.append(f"{desc.structure[:34]:34s} n={desc.n_atoms_sim:5d} V={desc.volume:8.0f} t{tier} {mode:6s} "
                   f"{u:>16s} cpu={ev.cpu_s:6.1f}s est={ev.est_cpu_s:6.1f}s")
    return out


if __name__ == "__main__":
    sample = load_sample()
    with ProcessPoolExecutor(PROCS) as ex:
        for f in as_completed([ex.submit(run, d) for d in sample]):
            print("\n".join(f.result()), flush=True)

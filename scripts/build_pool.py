"""Tier-0 every CIF in data/cifs once and cache the descriptors in data/pool.jsonl.

Runs still pay for tier 0 inside their budget (oracle.tier0); this cache is for calibration
and for sampling benchmark structures without re-parsing 12k CIFs.
Usage: uv run python scripts/build_pool.py [n_procs]
"""
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

from sieve.oracle import PROJECT_ROOT, describe

CIFS = PROJECT_ROOT / "data" / "cifs"
PREPARED = PROJECT_ROOT / "data" / "prepared"


def one(path: str) -> dict:
    c0 = time.process_time()
    d = asdict(describe(path, PREPARED))
    d["tier0_cpu_s"] = time.process_time() - c0
    return d


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 9
    paths = sorted(str(p) for p in CIFS.glob("*.cif"))
    t0, ok = time.time(), 0
    with open(PROJECT_ROOT / "data" / "pool.jsonl", "w") as f, ProcessPoolExecutor(n) as ex:
        for i, d in enumerate(ex.map(one, paths, chunksize=32)):
            f.write(json.dumps(d) + "\n")
            ok += d["parse_ok"]
            if i % 2000 == 0:
                print(f"{i}/{len(paths)}  ok={ok}  {time.time() - t0:.0f}s", flush=True)
    print(f"done {len(paths)} structures, {ok} usable, {time.time() - t0:.0f}s wall")

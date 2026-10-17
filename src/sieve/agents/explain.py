"""Compatibility alias: the analyst lives in sieve.agents.analyst."""
from sieve.agents.analyst import dossier, explain, explain_sync, pool_percentiles  # noqa: F401

if __name__ == "__main__":
    import sys

    for c in explain_sync(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 5):
        print(f"#{c['rank']} {c['structure']}: {c['headline']}")

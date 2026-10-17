#!/usr/bin/env bash
# Mirror a remote run's database to data/cloud.db every 10 s, so the local dashboard can show it:
#   scripts/cloud/sync.sh user@host        then   SIEVE_DB=data/cloud.db uv run python -m sieve dashboard
# The copy is taken with sqlite's backup API on the remote side, so it is always consistent.
set -euo pipefail
HOST="$1"; ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
while true; do
  ssh "$HOST" "cd ~/sieve && sqlite3 data/runs.db \".backup data/runs.snapshot.db\"" && \
    rsync -az "$HOST:sieve/data/runs.snapshot.db" "$ROOT/data/cloud.db.tmp" && mv "$ROOT/data/cloud.db.tmp" "$ROOT/data/cloud.db"
  sleep 10
done

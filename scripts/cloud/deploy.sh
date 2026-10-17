#!/usr/bin/env bash
# Run a SIEVE campaign on a remote Linux VM (e.g. a Crusoe Cloud CPU instance) and watch it from
# the local dashboard.
#
#   scripts/cloud/deploy.sh user@host [run_id] [budget_h]
#
# What it does, all over SSH; nothing here creates or pays for cloud resources:
#   1. copies the repo (code, patches, CoRE CIFs + CSV, calibration DB, .env) to ~/sieve
#   2. installs uv, fetches + patches RASPA2, builds it (Linux streams natively; patches still apply)
#   3. starts `python -m sieve sieve` under nohup with workers = remote cores - 1
#   4. leaves the run going; use sync.sh to mirror its database back for the dashboard
set -euo pipefail
HOST="$1"; RUN_ID="${2:-sieve-cloud}"; BUDGET_H="${3:-4}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
echo "== copying project to $HOST:~/sieve"
rsync -az --delete --exclude .venv --exclude vendor --exclude 'data/*.db*' --exclude data/prepared \
  --exclude data/viz --exclude data/shots --exclude data/logs --exclude .git \
  "$ROOT/" "$HOST:sieve/"
rsync -az "$ROOT/data/bench/modes.db" "$HOST:sieve/data/bench/"
echo "== remote setup"
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd ~/sieve
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
command -v gcc >/dev/null || (sudo apt-get update -y && sudo apt-get install -y build-essential)
./scripts/fetch_raspa.sh
uv sync
uv run python -W ignore -m sieve.oracle smoke
REMOTE
echo "== starting $RUN_ID ($BUDGET_H CPU-h)"
ssh "$HOST" "cd ~/sieve && export PATH=\$HOME/.local/bin:\$PATH && mkdir -p data/logs && \
  W=\$((\$(nproc) - 1)) && nohup uv run python -W ignore -m sieve --db data/runs.db sieve --budget-h $BUDGET_H \
  --workers \$W --calibration data/bench/modes.db --run-id $RUN_ID > data/logs/$RUN_ID.log 2>&1 < /dev/null & disown; \
  echo started with \$W workers"

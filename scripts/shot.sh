#!/usr/bin/env bash
# Headless full-size screenshot of the dashboard: scripts/shot.sh <run_id> <out.png> [width] [height]
set -euo pipefail
P=$(mktemp -d); trap 'rm -rf "$P"' EXIT
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --use-angle=swiftshader --enable-unsafe-swiftshader --user-data-dir="$P" \
  --hide-scrollbars --window-size="${3:-1440},${4:-900}" --virtual-time-budget=20000 --screenshot="$2" \
  "http://127.0.0.1:${PORT:-8770}/${PAGE:-}?run=$1&snapshot=1" 2>/dev/null

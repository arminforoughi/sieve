#!/usr/bin/env bash
# Fetch the RASPA2 2.0.4 sdist from PyPI, verify it, and apply SIEVE's patches into vendor/.
# `uv sync` then builds it with the CFLAGS pinned in pyproject.toml.
#
# Patches (patches/raspa2-2.0.4-sieve.patch):
#   potentials.c  PotentialThirdDerivative ignores LENNARD_JONES_CONTINUOUS_FRACTIONAL, which is the
#                 type RASPA assigns every LJ pair; MakeGrid therefore wrote all-zero energy grids.
#   grids.c       MakeGrid with ChargeMethod None crashed writing a Coulomb grid that it does not need.
set -euo pipefail
cd "$(dirname "$0")/.."
URL=https://files.pythonhosted.org/packages/df/7c/6b435fed5080134b1bdeecf82ce490a4d324f3fb87a432b226623f7cd69f/RASPA2-2.0.4.tar.gz
SHA=b051782dddc395b008c31f47347da344b202ac3aa909610e3ce67e94c51f2bf1
DEST=vendor/raspa2-2.0.4
if [ -f "$DEST/.sieve-patched" ]; then echo "$DEST already patched"; exit 0; fi
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
curl -sfL -o "$tmp/raspa2.tar.gz" "$URL"
echo "$SHA  $tmp/raspa2.tar.gz" | shasum -a 256 -c -
mkdir -p "$tmp/x" && tar -xzf "$tmp/raspa2.tar.gz" -C "$tmp/x"
rm -rf "$DEST" && mkdir -p vendor && mv "$tmp"/x/RASPA2-2.0.4 "$DEST"
patch -d "$DEST" -p1 < patches/raspa2-2.0.4-sieve.patch
touch "$DEST/.sieve-patched"
echo "patched RASPA2 in $DEST"

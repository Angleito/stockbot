#!/usr/bin/env bash
# Strict offline routing harness: deterministic, no network.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONHASHSEED=0
export FINRA_USE_MOCK=1
export BROKER_ENABLED=0
TMPDIR_HARNESS="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_HARNESS"' EXIT
export RUNS_DB_PATH="$TMPDIR_HARNESS/runs.sqlite"
if [[ -x venv/bin/python ]]; then PYBIN="venv/bin/python"; else PYBIN="python3"; fi
"$PYBIN" scripts/strict_routing_harness.py

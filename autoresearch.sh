#!/usr/bin/env bash
# Registry/static checks + generated confusion benchmark (no frozen holdout tuning).
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONHASHSEED=0
export FINRA_USE_MOCK=1
export BROKER_ENABLED=0
TMPDIR_HARNESS="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_HARNESS"' EXIT
export RUNS_DB_PATH="$TMPDIR_HARNESS/runs.sqlite"
if [[ -x venv/bin/python ]]; then PYBIN="venv/bin/python"; else PYBIN="python3"; fi
"$PYBIN" scripts/verify_tool_registry.py
"$PYBIN" scripts/strict_routing_harness.py
PYTHONHASHSEED=0 FINRA_USE_MOCK=1 BROKER_ENABLED=0 "$PYBIN" scripts/verify_pi_tools.py --confusion

#!/usr/bin/env bash
# One-command farm-heuristic experiment run.
#
# Thin wrapper around scripts/experiment_runner.py: it just picks the repo's
# venv interpreter (Windows or POSIX) and forwards every argument. The runner
# reads a JSON config (weights + seeds + depth), plays N farm games in parallel
# (GOMAXPROCS=1, 2048 taboo), and writes a self-contained result folder under
# research/experiments/<run-id>/ (config snapshot, results.jsonl, aggregates,
# perf.json, summary.md).
#
# Usage (from project/):
#   scripts/experiment.sh config/experiments/baseline.json
#   scripts/experiment.sh config/experiments/baseline.json --machine m4pro --workers 13
#   scripts/experiment.sh config/experiments/baseline.json --machine m4pro --shard 0/2
#   scripts/experiment.sh config/experiments/jackpot.json  --record-db
#
# Split ONE run across TWO machines (same config, disjoint seeds):
#   # on the Mac:      scripts/experiment.sh config/experiments/baseline.json --machine m4pro   --shard 0/2
#   # on Windows:      scripts/experiment.sh config/experiments/baseline.json --machine windows --shard 1/2
#   # then merge:      scripts/experiment_merge.py research/experiments/RUN_MAC research/experiments/RUN_WIN
#
# Every flag after the config is passed straight through to the runner
# (--machine, --workers, --shard IDX/COUNT, --engine-bin, --record-db, --out).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # -> project/
cd "$here"

# Pick the venv interpreter: Windows (Scripts/python.exe) or POSIX (bin/python).
if [[ -x ".venv/Scripts/python.exe" ]]; then
    PY=".venv/Scripts/python.exe"
elif [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
else
    echo "error: no venv found at .venv/ — create it first (see docs/experiments.md)" >&2
    exit 1
fi

if [[ $# -lt 1 ]]; then
    echo "usage: scripts/experiment.sh CONFIG.json [--machine L] [--workers N] [--shard I/C] ..." >&2
    exit 2
fi

exec "$PY" scripts/experiment_runner.py "$@"

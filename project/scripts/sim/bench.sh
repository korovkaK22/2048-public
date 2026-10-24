#!/usr/bin/env bash
# Strategy benchmark for monobank-2048 (SCORE-maximising, live-latency aware).
#
# Runs every strategy on the CLASSIC SpawnModel (P(4)=0.10) into data/bench.db,
# then the depth-3 heuristic-weight sweep via scripts/sim/bench_weights.py. Fixed
# seeds (base 1000) => reproducible. data/ is gitignored; do NOT commit the DB.
#
# Spawn-model caveat: the monobank clone's true P(4) is still TBD from live
# research. Re-run once measured (see docs/rules-research.md).
#
# Usage (from anywhere; picks the venv interpreter, Windows or POSIX):
#   bash scripts/sim/bench.sh
set -euo pipefail
cd "$(dirname "$0")/../.."   # scripts/sim/ -> project/

# Pick the venv interpreter: Windows (Scripts/python.exe) or POSIX (bin/python).
if [[ -x ".venv/Scripts/python.exe" ]]; then
    PY=".venv/Scripts/python.exe"
elif [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
else
    echo "error: no venv found at .venv/ — create it first (see docs/experiments.md)" >&2
    exit 1
fi
# `python -m bot2048.cli` rather than the `bot2048` entry point: in a worktree
# the entry point is the main copy's editable install, the module is THIS one.
BOT="$PY -m bot2048.cli"
DB=data/bench.db
SEED=1000

rm -f "$DB" "$DB-wal" "$DB-shm"
echo "### baselines (500 games each) ###"
$BOT sim run --strategy random --games 500 --seed $SEED --db "$DB"
$BOT sim run --strategy greedy --games 500 --seed $SEED --db "$DB"
$BOT sim run --strategy corner --games 500 --seed $SEED --db "$DB"

echo "### expectimax depth 2 (250 games) ###"
$BOT sim run --strategy expectimax --depth 2 --games 250 --seed $SEED --db "$DB"

echo "### expectimax depth 3 weight sweep: default / empty_heavy / snake_heavy (50 games each) ###"
$PY scripts/sim/bench_weights.py --games 50 --depth 3 --seed $SEED --db "$DB"

echo "### expectimax depth 4 (12 games; latency reference only) ###"
$BOT sim run --strategy expectimax --depth 4 --games 12 --seed $SEED --db "$DB"

echo "### ALL DONE ###"
$BOT db report --db "$DB"

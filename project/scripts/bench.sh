#!/usr/bin/env bash
# Strategy benchmark for monobank-2048 (SCORE-maximising, live-latency aware).
#
# Runs every strategy on the CLASSIC SpawnModel (P(4)=0.10) into data/bench.db,
# then the depth-3 heuristic-weight sweep via scripts/bench_weights.py. Fixed
# seeds (base 1000) => reproducible. data/ is gitignored; do NOT commit the DB.
#
# Spawn-model caveat: the monobank clone's true P(4) is still TBD from live
# research. Re-run once measured (see docs/rules-research.md).
#
# Usage (from project/):  ./.venv/Scripts/python.exe is the venv interpreter.
#   bash scripts/bench.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=./.venv/Scripts/python.exe
BOT=./.venv/Scripts/bot2048
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
$PY scripts/bench_weights.py --games 50 --depth 3 --seed $SEED --db "$DB"

echo "### expectimax depth 4 (12 games; latency reference only) ###"
$BOT sim run --strategy expectimax --depth 4 --games 12 --seed $SEED --db "$DB"

echo "### ALL DONE ###"
$BOT db report --db "$DB"

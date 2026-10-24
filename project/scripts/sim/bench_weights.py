#!/usr/bin/env python
"""Weight-tuning driver for ExpectimaxStrategy.

The `bot2048 sim` CLI hard-codes DEFAULT_WEIGHTS, so this helper drives the
public ExpectimaxStrategy API directly with alternative HeuristicWeights and
records full games into the same telemetry DB (one experiment per weight set).
It does NOT modify any source under core/ai/sim/storage — it only *uses* them.

Usage (from project/):
    ./.venv/Scripts/python.exe scripts/sim/bench_weights.py --games 60 --depth 3 \
        --seed 1000 --db data/bench.db
"""

from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path

from bot2048.ai.expectimax import ExpectimaxStrategy
from bot2048.ai.heuristics import HeuristicWeights
from bot2048.core.types import SpawnModel
from bot2048.sim.runner import play_games
from bot2048.storage import ExperimentMeta, SqliteRecorder

# label -> weights. "default" mirrors ai/heuristics.DEFAULT_WEIGHTS.
WEIGHT_SETS: dict[str, HeuristicWeights] = {
    "default": HeuristicWeights(empty=270.0, monotonicity=47.0, smoothness=11.0, corner=700.0),
    "empty_heavy": HeuristicWeights(empty=420.0, monotonicity=30.0, smoothness=8.0, corner=500.0),
    "snake_heavy": HeuristicWeights(empty=200.0, monotonicity=120.0, smoothness=25.0, corner=1100.0),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=60)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--seed", type=int, default=1000)
    parser.add_argument("--db", type=Path, default=Path("data/bench.db"))
    parser.add_argument("--max-moves", type=int, default=None)
    args = parser.parse_args()

    spawn_model = SpawnModel()  # classic 90/10
    seeds = [args.seed + i for i in range(args.games)]

    for label, weights in WEIGHT_SETS.items():
        strat = ExpectimaxStrategy(depth=args.depth, weights=weights, spawn_model=spawn_model)
        experiment_id = uuid.uuid4().hex
        params = {
            "depth": args.depth,
            "seed": args.seed,
            "weights_label": label,
            "weights": {
                "empty": weights.empty,
                "monotonicity": weights.monotonicity,
                "smoothness": weights.smoothness,
                "corner": weights.corner,
            },
        }
        meta = ExperimentMeta(
            experiment_id=experiment_id,
            kind="sim",
            strategy=f"expectimax(depth={args.depth})[w={label}]",
            params=params,
        )
        args.db.parent.mkdir(parents=True, exist_ok=True)
        recorder = SqliteRecorder(args.db, meta)
        started = time.perf_counter()
        try:
            summaries = play_games(
                strat,
                spawn_model,
                seeds=seeds,
                recorder=recorder,
                max_moves=args.max_moves,
                experiment_id=experiment_id,
                params=params,
            )
        finally:
            recorder.close()
        wall = time.perf_counter() - started
        mean_score = sum(s.final_score for s in summaries) / len(summaries)
        print(
            f"[{label:12s}] exp={experiment_id} games={len(summaries)} "
            f"mean_score={mean_score:,.0f} wall={wall:,.1f}s"
        )


if __name__ == "__main__":
    main()

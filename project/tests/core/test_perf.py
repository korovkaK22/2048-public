"""Soft performance benchmark: the engine must stay comfortably fast.

Target is >=50k moves/s; we assert a conservative 20k/s floor so slow CI
machines don't flake, but a real regression (e.g. losing the row tables)
still fails loudly. Skipping is NOT allowed.
"""

from __future__ import annotations

import random
import time

from bot2048.core import engine
from bot2048.core.types import Board, Direction, SpawnModel

FLOOR_MOVES_PER_S = 20_000


def _sample_positions(n: int) -> list[tuple[Board, Direction]]:
    """Realistic mid-game positions gathered by random self-play."""
    rng = random.Random(1234)
    model = SpawnModel()
    board, _ = engine.new_game(model, rng)
    positions: list[tuple[Board, Direction]] = []
    while len(positions) < n:
        legal = engine.legal_moves(board)
        if not legal:
            board, _ = engine.new_game(model, rng)
            continue
        direction = legal[rng.randrange(len(legal))]
        positions.append((board, direction))
        result = engine.move(board, direction)
        board, _spawn = engine.spawn_random(result.board, model, rng)
    return positions


def test_engine_throughput_floor() -> None:
    positions = _sample_positions(500)
    move = engine.move

    # warm-up (tables are built at import; this warms caches/JIT-less paths)
    for board, direction in positions:
        move(board, direction)

    done = 0
    start = time.perf_counter()
    while done < 60_000:
        for board, direction in positions:
            move(board, direction)
        done += len(positions)
    elapsed = time.perf_counter() - start

    moves_per_s = done / elapsed
    assert moves_per_s >= FLOOR_MOVES_PER_S, (
        f"engine too slow: {moves_per_s:,.0f} moves/s < {FLOOR_MOVES_PER_S:,} floor"
    )

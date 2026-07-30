"""Shared fixtures: a synthetic event stream and a recorded tmp database."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot2048.core.types import Board, Direction, Spawn
from bot2048.recording import GameEnded, GameEvent, GameStarted, MovePlayed, TileSpawned
from bot2048.storage import ExperimentMeta, SqliteRecorder

EXPERIMENT_ID = "exp-1"
GAME_ID = "game-0001"

EXPERIMENT = ExperimentMeta(
    experiment_id=EXPERIMENT_ID,
    kind="sim",
    strategy="greedy",
    params={"depth": 1, "seed": 42},
    notes="unit-test experiment",
)


def board_with(cells: dict[int, int]) -> Board:
    """Board with the given {cell_index: exponent} set, all else empty."""
    return tuple(cells.get(i, 0) for i in range(16))


B_INIT1 = board_with({3: 1})
B_INIT2 = board_with({3: 1, 12: 1})
B_M0 = board_with({0: 1, 12: 1})
B_S0 = board_with({0: 1, 12: 1, 15: 2})
B_M1 = board_with({0: 2, 3: 2})
B_S1 = board_with({0: 2, 3: 2, 8: 1})
B_M2 = board_with({12: 2, 15: 2, 13: 1})
B_S2 = board_with({12: 2, 15: 2, 13: 1, 2: 1})


def synthetic_events(
    game_id: str = GAME_ID, experiment_id: str = EXPERIMENT_ID
) -> list[GameEvent]:
    """A short, fully-populated game: start, 2 initial spawns, 3 moves, end."""
    return [
        GameStarted(
            game_id=game_id,
            experiment_id=experiment_id,
            kind="sim",
            strategy="greedy",
            seed=42,
            initial_board=B_INIT2,
            params={"depth": 1},
            ts=100.0,
        ),
        TileSpawned(
            game_id=game_id, ply=-1, spawn=Spawn(cell=3, exponent=1),
            board_after=B_INIT1, ts=100.0,
        ),
        TileSpawned(
            game_id=game_id, ply=-2, spawn=Spawn(cell=12, exponent=1),
            board_after=B_INIT2, ts=100.0,
        ),
        MovePlayed(
            game_id=game_id, ply=0, direction=Direction.LEFT,
            board_before=B_INIT2, board_after=B_M0,
            score_gained=0, score_total=0, eval_ms=1.25,
            search_depth=None, eval_details={}, ts=100.1,
        ),
        TileSpawned(
            game_id=game_id, ply=0, spawn=Spawn(cell=15, exponent=2),
            board_after=B_S0, ts=100.1,
        ),
        MovePlayed(
            game_id=game_id, ply=1, direction=Direction.UP,
            board_before=B_S0, board_after=B_M1,
            score_gained=4, score_total=4, eval_ms=2.5,
            search_depth=3, eval_details={"heuristics": {"empty": 14.0}, "candidates": [0, 3]},
            ts=100.2,
        ),
        TileSpawned(
            game_id=game_id, ply=1, spawn=Spawn(cell=8, exponent=1),
            board_after=B_S1, ts=100.2,
        ),
        MovePlayed(
            game_id=game_id, ply=2, direction=Direction.DOWN,
            board_before=B_S1, board_after=B_M2,
            score_gained=0, score_total=4, eval_ms=0.75,
            search_depth=3, eval_details={"note": "final move"}, ts=100.3,
        ),
        TileSpawned(
            game_id=game_id, ply=2, spawn=Spawn(cell=2, exponent=1),
            board_after=B_S2, ts=100.3,
        ),
        GameEnded(
            game_id=game_id, final_score=4, max_exponent=2, num_moves=3,
            end_reason="aborted", duration_s=1.5, ts=101.5,
        ),
    ]


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "games.db"


@pytest.fixture()
def synthetic_db(db_path: Path) -> Path:
    """A database containing one fully recorded synthetic game."""
    with SqliteRecorder(db_path, EXPERIMENT) as recorder:
        for event in synthetic_events():
            recorder.record(event)
    return db_path

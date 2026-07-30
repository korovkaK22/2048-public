"""Unit tests for the pure 2048 engine: fixtures, scoring, legality, game over."""

from __future__ import annotations

import random

import pytest

from bot2048.core import engine
from bot2048.core.types import (
    EMPTY_BOARD,
    Direction,
    SpawnModel,
    board_from_values,
    board_values,
)

# --- move fixtures ---------------------------------------------------------
# Each case: (values before, direction, values after, score gained)
MOVE_CASES = [
    # simple slide, no merge
    ((0, 2, 0, 0) + (0,) * 12, Direction.LEFT, (2, 0, 0, 0) + (0,) * 12, 0),
    ((0, 2, 0, 0) + (0,) * 12, Direction.RIGHT, (0, 0, 0, 2) + (0,) * 12, 0),
    # a pair merges once
    ((2, 2, 0, 0) + (0,) * 12, Direction.LEFT, (4, 0, 0, 0) + (0,) * 12, 4),
    # four equal tiles -> two merges, not one 8
    ((2, 2, 2, 2) + (0,) * 12, Direction.LEFT, (4, 4, 0, 0) + (0,) * 12, 8),
    # merge resolves from the leading edge: LEFT merges the left pair first
    ((2, 2, 2, 0) + (0,) * 12, Direction.LEFT, (4, 2, 0, 0) + (0,) * 12, 4),
    ((2, 2, 2, 0) + (0,) * 12, Direction.RIGHT, (0, 0, 2, 4) + (0,) * 12, 4),
    # no double merge: 4 4 2 2 -> 8 4, NOT 16
    ((4, 4, 2, 2) + (0,) * 12, Direction.LEFT, (8, 4, 0, 0) + (0,) * 12, 12),
    # gap between equal tiles still merges
    ((2, 0, 0, 2) + (0,) * 12, Direction.LEFT, (4, 0, 0, 0) + (0,) * 12, 4),
    # vertical moves
    (
        (2, 0, 0, 0, 2, 0, 0, 0, 4, 0, 0, 0, 0, 0, 0, 0),
        Direction.UP,
        (4, 0, 0, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        4,
    ),
    (
        (2, 0, 0, 0, 2, 0, 0, 0, 4, 0, 0, 0, 4, 0, 0, 0),
        Direction.DOWN,
        (0, 0, 0, 0, 0, 0, 0, 0, 4, 0, 0, 0, 8, 0, 0, 0),
        12,
    ),
]


@pytest.mark.parametrize(("before", "direction", "after", "score"), MOVE_CASES)
def test_move_fixtures(
    before: tuple[int, ...], direction: Direction, after: tuple[int, ...], score: int
) -> None:
    result = engine.move(board_from_values(before), direction)
    assert board_values(result.board) == after
    assert result.score_gained == score
    assert result.moved is True


def test_illegal_move_detected() -> None:
    board = board_from_values((2, 4, 2, 4) + (0,) * 12)
    result = engine.move(board, Direction.UP)
    assert result.moved is False
    assert result.board == board
    assert result.score_gained == 0


def test_legal_moves_and_game_over() -> None:
    # checkerboard: no merges, no empties -> dead
    dead = board_from_values((2, 4, 2, 4, 4, 2, 4, 2, 2, 4, 2, 4, 4, 2, 4, 2))
    assert engine.legal_moves(dead) == []
    assert engine.is_game_over(dead)

    # same but with one mergeable pair -> alive
    alive = board_from_values((2, 4, 2, 4, 4, 2, 4, 2, 2, 4, 2, 4, 4, 2, 4, 4))
    assert not engine.is_game_over(alive)
    assert set(engine.legal_moves(alive)) <= set(Direction)
    assert engine.legal_moves(alive)


def test_score_accounting_accumulates_created_tile_values() -> None:
    # (8 8) (4 4) LEFT -> 16 + 8 = 24
    board = board_from_values((8, 8, 4, 4) + (0,) * 12)
    result = engine.move(board, Direction.LEFT)
    assert result.score_gained == 24
    assert board_values(result.board)[:2] == (16, 8)


def test_empty_cells_and_max_exponent() -> None:
    board = board_from_values((2, 0, 0, 0) + (0,) * 11 + (32,))
    empties = engine.empty_cells(board)
    assert len(empties) == 14
    assert 0 not in empties
    assert 15 not in empties
    assert engine.max_exponent(board) == 5
    assert engine.max_exponent(EMPTY_BOARD) == 0


def test_spawn_random_deterministic_and_valid() -> None:
    model = SpawnModel()
    board = board_from_values((2, 0, 0, 0) + (0,) * 12)
    b1, s1 = engine.spawn_random(board, model, random.Random(7))
    b2, s2 = engine.spawn_random(board, model, random.Random(7))
    assert (b1, s1) == (b2, s2)
    assert board[s1.cell] == 0
    assert b1[s1.cell] == s1.exponent
    assert s1.exponent in {1, 2}
    assert sum(1 for c in b1 if c) == 2


def test_spawn_random_exponent_distribution() -> None:
    model = SpawnModel()
    rng = random.Random(123)
    counts = {1: 0, 2: 0}
    for _ in range(5000):
        _, spawn = engine.spawn_random(EMPTY_BOARD, model, rng)
        counts[spawn.exponent] += 1
    assert 0.85 < counts[1] / 5000 < 0.95


def test_spawn_on_full_board_raises() -> None:
    full = board_from_values((2, 4, 2, 4, 4, 2, 4, 2, 2, 4, 2, 4, 4, 2, 4, 2))
    with pytest.raises(engine.NoEmptyCellsError):
        engine.spawn_random(full, SpawnModel(), random.Random(0))


def test_new_game() -> None:
    board, spawns = engine.new_game(SpawnModel(), random.Random(99))
    assert len(spawns) == 2
    assert sum(1 for c in board if c) == 2
    for spawn in spawns:
        assert board[spawn.cell] == spawn.exponent

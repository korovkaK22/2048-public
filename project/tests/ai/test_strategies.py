"""Tests for baseline strategies, heuristics and the Strategy contract."""

from __future__ import annotations

import random

import pytest

from bot2048.ai.heuristics import DEFAULT_WEIGHTS, HeuristicWeights, breakdown, evaluate
from bot2048.ai.strategies import (
    CornerStrategy,
    GreedyStrategy,
    NoLegalMoveError,
    RandomStrategy,
    Strategy,
)
from bot2048.core import engine
from bot2048.core.types import Direction, board_from_values

DEAD_BOARD = board_from_values((2, 4, 2, 4, 4, 2, 4, 2, 2, 4, 2, 4, 4, 2, 4, 2))
SIMPLE_BOARD = board_from_values((2, 2, 0, 0) + (0,) * 12)


def test_strategies_satisfy_protocol() -> None:
    for strat in (RandomStrategy(), GreedyStrategy(), CornerStrategy()):
        assert isinstance(strat, Strategy)
        assert isinstance(strat.name, str) and strat.name


BASELINES = [RandomStrategy(random.Random(0)), GreedyStrategy(), CornerStrategy()]


@pytest.mark.parametrize("strat", BASELINES)
def test_choose_returns_legal_move(strat: RandomStrategy | GreedyStrategy | CornerStrategy) -> None:
    decision = strat.choose(SIMPLE_BOARD)
    assert decision.direction in engine.legal_moves(SIMPLE_BOARD)
    assert decision.eval_ms >= 0.0


@pytest.mark.parametrize("strat", BASELINES)
def test_choose_raises_on_dead_board(
    strat: RandomStrategy | GreedyStrategy | CornerStrategy,
) -> None:
    with pytest.raises(NoLegalMoveError):
        strat.choose(DEAD_BOARD)


def test_random_strategy_is_seed_deterministic() -> None:
    picks_a = [RandomStrategy(random.Random(5)).choose(SIMPLE_BOARD).direction for _ in range(10)]
    picks_b = [RandomStrategy(random.Random(5)).choose(SIMPLE_BOARD).direction for _ in range(10)]
    assert picks_a == picks_b


def test_greedy_prefers_the_scoring_move() -> None:
    # LEFT/RIGHT merge (score 4); UP/DOWN just slide (score 0)
    board = board_from_values((2, 2, 0, 0) + (0,) * 12)
    decision = GreedyStrategy().choose(board)
    assert decision.direction in {Direction.LEFT, Direction.RIGHT}
    assert decision.details["values"]


def test_corner_follows_priority() -> None:
    # all four moves legal -> priority head (DOWN) wins
    board = board_from_values((0, 2, 0, 0, 0, 0, 2, 0) + (0,) * 8)
    assert CornerStrategy().choose(board).direction == Direction.DOWN


def test_corner_rejects_bad_priority() -> None:
    with pytest.raises(ValueError):
        CornerStrategy(priority=(Direction.UP, Direction.UP, Direction.LEFT, Direction.RIGHT))


def test_heuristics_prefer_empty_boards() -> None:
    sparse = board_from_values((2, 2, 0, 0) + (0,) * 12)
    crowded = board_from_values((2, 4, 2, 4, 4, 2, 4, 2, 2, 4, 2, 4, 4, 2, 4, 2))
    assert evaluate(sparse) > evaluate(crowded)


def test_heuristics_corner_bonus() -> None:
    in_corner = board_from_values((64, 2, 0, 0) + (0,) * 12)
    in_middle = board_from_values((0, 2, 0, 0, 0, 64, 0, 0) + (0,) * 8)
    weights = HeuristicWeights(empty=0.0, monotonicity=0.0, smoothness=0.0, corner=1.0)
    assert evaluate(in_corner, weights) == 6.0  # exponent of 64
    assert evaluate(in_middle, weights) == 0.0


def test_breakdown_matches_evaluate() -> None:
    board = board_from_values((2, 4, 8, 16, 0, 0, 2, 0, 0, 0, 0, 0, 2, 0, 0, 4))
    parts = breakdown(board)
    assert parts["total"] == pytest.approx(evaluate(board, DEFAULT_WEIGHTS))
    assert set(parts) == {"empty", "monotonicity", "smoothness", "corner", "total"}

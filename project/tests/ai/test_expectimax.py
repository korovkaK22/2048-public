"""Expectimax: contract, determinism, adaptive depth, and strength vs random."""

from __future__ import annotations

import random
import statistics

import pytest

from bot2048.ai.expectimax import ExpectimaxStrategy
from bot2048.ai.strategies import NoLegalMoveError, RandomStrategy, Strategy
from bot2048.core import engine
from bot2048.core.types import SpawnModel, board_from_values
from bot2048.recording import ListRecorder
from bot2048.sim.runner import play_game

DEAD_BOARD = board_from_values((2, 4, 2, 4, 4, 2, 4, 2, 2, 4, 2, 4, 4, 2, 4, 2))


def test_satisfies_protocol_and_name() -> None:
    strat = ExpectimaxStrategy(depth=4)
    assert isinstance(strat, Strategy)
    assert strat.name == "expectimax(depth=4)"


def test_rejects_bad_depth() -> None:
    with pytest.raises(ValueError):
        ExpectimaxStrategy(depth=0)


def test_choose_returns_legal_move_with_telemetry() -> None:
    board = board_from_values((2, 2, 4, 0, 0, 4, 0, 0) + (0,) * 8)
    decision = ExpectimaxStrategy(depth=2).choose(board)
    assert decision.direction in engine.legal_moves(board)
    assert decision.search_depth == 2
    assert decision.eval_ms >= 0.0
    assert set(decision.details) >= {"move_values", "heuristic", "empty_cells"}
    assert decision.details["move_values"]  # one entry per legal move


def test_choose_raises_on_dead_board() -> None:
    with pytest.raises(NoLegalMoveError):
        ExpectimaxStrategy(depth=2).choose(DEAD_BOARD)


def test_choose_is_deterministic() -> None:
    board = board_from_values((2, 2, 4, 8, 0, 4, 2, 0, 0, 0, 2, 0, 0, 0, 0, 2))
    strat = ExpectimaxStrategy(depth=2)
    first = strat.choose(board)
    second = strat.choose(board)
    assert first.direction == second.direction
    assert first.details["move_values"] == second.details["move_values"]


def test_adaptive_depth_kicks_in_when_cramped() -> None:
    # 3 empty cells <= threshold 4 -> searched depth = depth + 1
    cramped = board_from_values((2, 4, 2, 4, 4, 2, 4, 2, 2, 4, 2, 0, 4, 2, 0, 0))
    decision = ExpectimaxStrategy(depth=1).choose(cramped)
    assert decision.search_depth == 2

    roomy = board_from_values((2, 2, 0, 0) + (0,) * 12)
    assert ExpectimaxStrategy(depth=1).choose(roomy).search_depth == 1


def test_expectimax_beats_random_by_wide_margin() -> None:
    """5 seeded games each, depth 2: expectimax must crush random."""
    model = SpawnModel()
    seeds = list(range(5))
    max_moves = 250

    expecti_scores: list[int] = []
    random_scores: list[int] = []
    for seed in seeds:
        rec = ListRecorder()
        summary = play_game(
            ExpectimaxStrategy(depth=2, spawn_model=model),
            model,
            seed,
            rec,
            max_moves=max_moves,
        )
        expecti_scores.append(summary.final_score)

        rec2 = ListRecorder()
        summary2 = play_game(
            RandomStrategy(random.Random(seed)), model, seed, rec2, max_moves=max_moves
        )
        random_scores.append(summary2.final_score)

    mean_expecti = statistics.mean(expecti_scores)
    mean_random = statistics.mean(random_scores)
    assert mean_expecti > 2.0 * mean_random, (
        f"expectimax mean {mean_expecti:.0f} not >2x random mean {mean_random:.0f}"
    )

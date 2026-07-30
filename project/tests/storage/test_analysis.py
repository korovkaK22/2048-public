"""Renderer smoke tests + stats/chi-square unit tests."""

from __future__ import annotations

import math

from conftest import board_with

from bot2048.analysis import (
    CHI2_CRIT_P01,
    chi_square_vs_model,
    max_tile_histogram,
    render_board,
    render_hex_board,
    score_stats,
)
from bot2048.storage import GameSummary, board_to_hex


def _summary(
    final_score: int | None, max_exponent: int | None, num_moves: int | None
) -> GameSummary:
    return GameSummary(
        game_id="g",
        experiment_id="e",
        seed=None,
        started_at=0.0,
        ended_at=None,
        final_score=final_score,
        max_exponent=max_exponent,
        num_moves=num_moves,
        end_reason=None,
        duration_s=None,
    )


def test_render_board_smoke() -> None:
    board = board_with({0: 1, 5: 11, 15: 2})
    text = render_board(board)
    lines = text.splitlines()
    assert len(lines) == 9  # 4 rows * 2 + closing border
    assert len({len(line) for line in lines}) == 1  # rectangular
    assert "2048" in text
    assert " 2 " in lines[1]
    assert "4" in lines[7]


def test_render_empty_board_has_no_digits() -> None:
    text = render_board((0,) * 16)
    assert not any(ch.isdigit() for ch in text)


def test_render_hex_board_matches_render_board() -> None:
    board = board_with({1: 3, 7: 5})
    assert render_hex_board(board_to_hex(board)) == render_board(board)


def test_chi_square_exact_match_is_zero() -> None:
    result = chi_square_vs_model({1: 900, 2: 100})
    assert result is not None
    assert result.statistic == 0.0
    assert result.degrees_of_freedom == 1
    assert result.total == 1000
    assert result.statistic <= CHI2_CRIT_P01[1]


def test_chi_square_detects_deviation() -> None:
    result = chi_square_vs_model({1: 500, 2: 500})
    assert result is not None
    expected = (500 - 900) ** 2 / 900 + (500 - 100) ** 2 / 100
    assert result.statistic == expected
    assert result.statistic > CHI2_CRIT_P01[result.degrees_of_freedom]


def test_chi_square_impossible_category_is_infinite() -> None:
    result = chi_square_vs_model({1: 90, 2: 9, 3: 1})
    assert result is not None
    assert math.isinf(result.statistic)


def test_chi_square_no_data() -> None:
    assert chi_square_vs_model({}) is None


def test_score_stats() -> None:
    summaries = [
        _summary(10, 3, 5),
        _summary(30, 4, 15),
        _summary(None, None, None),  # unfinished game
    ]
    stats = score_stats(summaries)
    assert stats is not None
    assert stats.games == 3
    assert stats.finished == 2
    assert stats.mean_score == 20.0
    assert stats.median_score == 20.0
    assert stats.max_score == 30
    assert stats.avg_moves == 10.0
    assert score_stats([_summary(None, None, None)]) is None
    assert score_stats([]) is None


def test_max_tile_histogram() -> None:
    summaries = [
        _summary(1, 3, 1),
        _summary(2, 3, 2),
        _summary(3, 5, 3),
        _summary(None, None, None),
    ]
    assert max_tile_histogram(summaries) == {3: 2, 5: 1}

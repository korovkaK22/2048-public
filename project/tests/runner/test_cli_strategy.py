"""Strategy selection in ``bot2048 live`` (``_make_strategy``).

Covers the new ``go`` option that routes to the Go bitboard engine and keeps
the same per-move telemetry the runner records into the ``moves`` table.
"""

from __future__ import annotations

import pytest

from bot2048.ai.expectimax import ExpectimaxStrategy
from bot2048.ai.go_engine import GoExpectimaxStrategy, find_engine_binary
from bot2048.core import engine
from bot2048.core.types import Board
from bot2048.runner.cli import _make_strategy


def test_make_strategy_expectimax() -> None:
    strat = _make_strategy("expectimax", 2)
    assert isinstance(strat, ExpectimaxStrategy)
    assert strat.name == "expectimax(depth=2)"


@pytest.mark.skipif(
    find_engine_binary() is None,
    reason="Go engine binary not built (run tools/engine2048/build.sh)",
)
def test_make_strategy_go_uses_engine_and_fills_move_telemetry() -> None:
    strat = _make_strategy("go", 3)
    try:
        assert isinstance(strat, GoExpectimaxStrategy)
        assert strat.name == "go-expectimax(depth=3)"
        # Two mergeable pairs + room: at least one legal move.
        board: Board = (1, 1, 2, 3, 0, 0, 1, 2, 0, 0, 0, 1, 0, 0, 0, 0)
        decision = strat.choose(board)
        assert decision.direction in engine.legal_moves(board)
        # The exact fields the runner persists per move (eval_ms /
        # search_depth / eval_details_json).
        assert decision.eval_ms >= 0.0
        assert decision.search_depth is not None and decision.search_depth >= 3
        assert decision.details["engine"] == "go"
    finally:
        strat.close()  # type: ignore[attr-defined]


@pytest.mark.skipif(
    find_engine_binary() is None,
    reason="Go engine binary not built (run tools/engine2048/build.sh)",
)
def test_make_strategy_go_budget_ms_threads_into_engine() -> None:
    strat = _make_strategy("go", 6, budget_ms=100)
    try:
        assert isinstance(strat, GoExpectimaxStrategy)
        assert strat.budget_ms == 100.0
        assert strat.name == "go-expectimax(budget=100ms)"
    finally:
        strat.close()  # type: ignore[attr-defined]


@pytest.mark.skipif(
    find_engine_binary() is None,
    reason="Go engine binary not built (run tools/engine2048/build.sh)",
)
def test_make_strategy_go_without_budget_keeps_fixed_depth() -> None:
    strat = _make_strategy("go", 6)
    try:
        assert isinstance(strat, GoExpectimaxStrategy)
        assert strat.budget_ms is None
        assert strat.name == "go-expectimax(depth=6)"
    finally:
        strat.close()  # type: ignore[attr-defined]

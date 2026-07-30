"""Strategy contract and the baseline (non-search) strategies.

A strategy turns a board into a :class:`Decision`. Strategies must only be
asked to choose on boards that have at least one legal move; otherwise
:class:`NoLegalMoveError` is raised.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from bot2048.core.engine import empty_cells, legal_moves, move
from bot2048.core.types import Board, Direction

__all__ = [
    "CornerStrategy",
    "Decision",
    "GreedyStrategy",
    "NoLegalMoveError",
    "RandomStrategy",
    "Strategy",
]


class NoLegalMoveError(Exception):
    """Raised when ``choose`` is called on a board with no legal moves."""


@dataclass(slots=True)
class Decision:
    """A strategy's chosen move plus telemetry for the recording layer."""

    direction: Direction
    eval_ms: float
    search_depth: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Strategy(Protocol):
    """Anything that can pick a move for a board."""

    @property
    def name(self) -> str:
        """Stable identifier, e.g. ``"expectimax(depth=4)"``. Stored in the DB."""
        ...

    def choose(self, board: Board) -> Decision:
        """Pick a legal move. Raises :class:`NoLegalMoveError` on dead boards."""
        ...


class RandomStrategy:
    """Uniformly random legal move. The floor every other strategy must beat."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng if rng is not None else random.Random()

    @property
    def name(self) -> str:
        return "random"

    def choose(self, board: Board) -> Decision:
        start = time.perf_counter()
        moves = legal_moves(board)
        if not moves:
            raise NoLegalMoveError("no legal moves available")
        direction = moves[self._rng.randrange(len(moves))]
        return Decision(direction=direction, eval_ms=(time.perf_counter() - start) * 1000.0)


class GreedyStrategy:
    """Maximise immediate score gained plus resulting empty-cell count."""

    @property
    def name(self) -> str:
        return "greedy"

    def choose(self, board: Board) -> Decision:
        start = time.perf_counter()
        best: Direction | None = None
        best_value = 0.0
        values: dict[str, float] = {}
        for direction in Direction:
            result = move(board, direction)
            if not result.moved:
                continue
            value = float(result.score_gained + len(empty_cells(result.board)))
            values[direction.name] = value
            if best is None or value > best_value:
                best, best_value = direction, value
        if best is None:
            raise NoLegalMoveError("no legal moves available")
        return Decision(
            direction=best,
            eval_ms=(time.perf_counter() - start) * 1000.0,
            details={"values": values},
        )


class CornerStrategy:
    """First legal move from a fixed priority that parks the big tile in a corner.

    The default priority (DOWN, LEFT, RIGHT, UP) gravitates tiles toward the
    bottom-left corner and only moves UP when nothing else is possible.
    """

    _DEFAULT_PRIORITY = (Direction.DOWN, Direction.LEFT, Direction.RIGHT, Direction.UP)

    def __init__(self, priority: tuple[Direction, ...] | None = None) -> None:
        chosen = self._DEFAULT_PRIORITY if priority is None else priority
        if set(chosen) != set(Direction) or len(chosen) != len(set(Direction)):
            raise ValueError("priority must be a permutation of all four directions")
        self._priority = chosen

    @property
    def name(self) -> str:
        return "corner"

    def choose(self, board: Board) -> Decision:
        start = time.perf_counter()
        legal = set(legal_moves(board))
        for direction in self._priority:
            if direction in legal:
                return Decision(
                    direction=direction,
                    eval_ms=(time.perf_counter() - start) * 1000.0,
                    details={"priority_rank": self._priority.index(direction)},
                )
        raise NoLegalMoveError("no legal moves available")

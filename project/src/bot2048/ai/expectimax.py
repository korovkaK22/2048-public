"""Expectimax search strategy over the 2048 bitboard engine.

Max nodes iterate over legal player moves; chance nodes average the
heuristic value over every (empty cell, spawn exponent) combination
weighted by the :class:`~bot2048.core.types.SpawnModel`. Chance branches
whose cumulative probability falls below ``PRUNE_PROBABILITY`` are cut
and replaced by an immediate heuristic evaluation.

A transposition table keyed by ``(bitboard, depth)`` memoises chance-node
values; it is cleared at the start of every :meth:`ExpectimaxStrategy.choose`
call and additionally size-capped so a single deep search cannot grow it
without bound. Depth adapts: when the board has <= 4 empty cells the
search runs one ply deeper (the endgame is where tactics matter and the
branching factor is smallest).
"""

from __future__ import annotations

import time
from typing import Final

from bot2048.ai.heuristics import (
    DEFAULT_WEIGHTS,
    HeuristicWeights,
    _breakdown_bb,
    _evaluate_bb,
)
from bot2048.ai.strategies import Decision, NoLegalMoveError
from bot2048.core.engine import _move_bb, _pack
from bot2048.core.types import Board, Direction, SpawnModel

__all__ = ["ExpectimaxStrategy"]

PRUNE_PROBABILITY: Final[float] = 1e-4
_ADAPTIVE_EMPTY_THRESHOLD: Final[int] = 4
_MAX_TABLE_ENTRIES: Final[int] = 200_000
_LOSS_VALUE: Final[float] = -1e9


def _empty_shifts(bb: int) -> list[int]:
    return [shift for shift in range(0, 64, 4) if not (bb >> shift) & 0xF]


class ExpectimaxStrategy:
    """Depth-limited expectimax with heuristic leaf evaluation."""

    def __init__(
        self,
        depth: int = 3,
        weights: HeuristicWeights = DEFAULT_WEIGHTS,
        spawn_model: SpawnModel | None = None,
    ) -> None:
        if depth < 1:
            raise ValueError("depth must be >= 1")
        self._depth = depth
        self._weights = weights
        self._spawn_model = spawn_model if spawn_model is not None else SpawnModel()
        # exponent-probability pairs; entries with ~zero mass dropped
        self._spawn_probs: tuple[tuple[int, float], ...] = tuple(
            (exp, prob) for exp, prob in self._spawn_model.exponent_probs if prob > 0.0
        )
        self._table: dict[tuple[int, int], float] = {}

    @property
    def name(self) -> str:
        return f"expectimax(depth={self._depth})"

    @property
    def depth(self) -> int:
        return self._depth

    def choose(self, board: Board) -> Decision:
        start = time.perf_counter()
        bb = _pack(board)
        empty_count = len(_empty_shifts(bb))
        depth = self._depth + (1 if empty_count <= _ADAPTIVE_EMPTY_THRESHOLD else 0)

        self._table.clear()
        best_dir: Direction | None = None
        best_value = 0.0
        move_values: dict[str, float] = {}
        for direction in Direction:
            child, _score = _move_bb(bb, direction)
            if child == bb:
                continue
            value = self._chance_value(child, depth - 1, 1.0)
            move_values[direction.name] = value
            if best_dir is None or value > best_value:
                best_dir, best_value = direction, value
        if best_dir is None:
            raise NoLegalMoveError("no legal moves available")

        eval_ms = (time.perf_counter() - start) * 1000.0
        details = {
            "move_values": move_values,
            "heuristic": _breakdown_bb(bb, self._weights),
            "empty_cells": empty_count,
            "table_size": len(self._table),
        }
        return Decision(
            direction=best_dir,
            eval_ms=eval_ms,
            search_depth=depth,
            details=details,
        )

    # --- internal search ---------------------------------------------------

    def _chance_value(self, bb: int, depth: int, probability: float) -> float:
        """Expected value over all spawns on ``bb`` (a post-move board)."""
        if depth <= 0 or probability < PRUNE_PROBABILITY:
            return _evaluate_bb(bb, self._weights)
        key = (bb, depth)
        cached = self._table.get(key)
        if cached is not None:
            return cached

        shifts = _empty_shifts(bb)
        cell_prob = 1.0 / len(shifts)  # engine guarantees >=1 empty after a move
        total = 0.0
        for shift in shifts:
            for exponent, exp_prob in self._spawn_probs:
                branch_prob = probability * cell_prob * exp_prob
                child = bb | (exponent << shift)
                if branch_prob < PRUNE_PROBABILITY:
                    total += cell_prob * exp_prob * _evaluate_bb(child, self._weights)
                else:
                    total += cell_prob * exp_prob * self._max_value(child, depth, branch_prob)

        if len(self._table) < _MAX_TABLE_ENTRIES:
            self._table[key] = total
        return total

    def _max_value(self, bb: int, depth: int, probability: float) -> float:
        """Best value over the player's legal moves; loss if there are none."""
        best: float | None = None
        for direction in Direction:
            child, _score = _move_bb(bb, direction)
            if child == bb:
                continue
            value = self._chance_value(child, depth - 1, probability)
            if best is None or value > best:
                best = value
        if best is None:
            return _LOSS_VALUE * probability
        return best

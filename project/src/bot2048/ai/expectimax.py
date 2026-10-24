"""Expectimax search strategy over the 2048 bitboard engine.

Max nodes iterate over legal player moves; chance nodes average the
heuristic value over every (empty cell, spawn exponent) combination
weighted by the :class:`~bot2048.core.types.SpawnModel`. Chance branches
whose cumulative probability falls below ``PRUNE_PROBABILITY`` are cut
and replaced by an immediate heuristic evaluation.

A transposition table keyed by ``(bitboard, depth)`` memoises chance-node
values; it is cleared before every search iteration and additionally
size-capped so a single deep search cannot grow it without bound. Depth
adapts: when the board has <= 4 empty cells the search runs one ply deeper
(the endgame is where tactics matter and the branching factor is smallest).

Search limits (mirrors the ``Limits``/``Guard`` vocabulary of the Go engines):

* ``depth`` -- the depth ceiling for a move (``max_depth``; the adaptive
  bonus raises it by one on cramped boards). ``None`` means "no ceiling" and
  is allowed only together with a budget or a node cap.
* ``budget_ms`` -- wall-clock ceiling per :meth:`ExpectimaxStrategy.choose`.
* ``max_nodes`` -- node ceiling per ``choose`` (hardware-independent). A node
  is an interior chance node, the same unit the Go engines count and report.

With no budget and no node cap the search is a single fixed-depth pass --
bit-identical to the historical depth-only strategy. With a limit set, the
search deepens iteratively ``1..max_depth``; a limit hit mid-iteration
discards that iteration and the last *completed* depth's decision stands
(depth 1 is never interrupted, so a legal move is always returned). The
limits are polled every ``_POLL_MASK + 1`` nodes and between iterations, so
``nodes`` overshoots ``max_nodes`` by at most ``_POLL_MASK``. A capped search
that completes its ceiling returns exactly what the depth-only search would.

``Decision.search_depth`` is the depth reached; ``details["search"]``
records ``{"stop": "depth"|"budget"|"nodes"|"ladder_end", "nodes", "max_depth",
"budget_ms", "max_nodes"}`` with the *effective* limits of that move.
"""

from __future__ import annotations

import math
import time
from typing import Any, Final

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
# Limits are polled when ``nodes & _POLL_MASK == 0``. A node is an *interior*
# chance node (as in the Go engines, which poll every 512); one costs Python
# ~0.1-0.5 ms (it expands 2*empties player nodes), so every 8 keeps the poll
# cadence at a few ms -- the overshoot past ``budget_ms``/``max_nodes``.
_POLL_MASK: Final[int] = 7
# Iterative deepening never goes past this ply even with ``depth=None``:
# a tiny finite tree (every line dies) would otherwise burn the budget on
# iterations that cannot change the answer. Reported as ``stop="ladder_end"``.
_DEEPENING_CEILING: Final[int] = 32


class _SearchAborted(Exception):
    """Raised from the poll when a budget/node limit is hit mid-iteration."""

    def __init__(self, stop: str) -> None:
        super().__init__(stop)
        self.stop = stop


def _empty_shifts(bb: int) -> list[int]:
    return [shift for shift in range(0, 64, 4) if not (bb >> shift) & 0xF]


class ExpectimaxStrategy:
    """Depth-, time- and node-limited expectimax with heuristic leaf evaluation."""

    def __init__(
        self,
        depth: int | None = 3,
        weights: HeuristicWeights = DEFAULT_WEIGHTS,
        spawn_model: SpawnModel | None = None,
        *,
        budget_ms: float | None = None,
        max_nodes: int | None = None,
        adaptive: bool = True,
    ) -> None:
        if depth is not None and depth < 1:
            raise ValueError("depth must be >= 1")
        if budget_ms is not None and not budget_ms > 0.0:
            raise ValueError("budget_ms must be > 0")
        if max_nodes is not None and max_nodes < 1:
            raise ValueError("max_nodes must be >= 1")
        if depth is None and budget_ms is None and max_nodes is None:
            raise ValueError("depth=None requires budget_ms or max_nodes")
        self._depth = depth
        self._budget_ms = budget_ms
        self._max_nodes = max_nodes
        self._adaptive = adaptive
        self._weights = weights
        self._spawn_model = spawn_model if spawn_model is not None else SpawnModel()
        # exponent-probability pairs; entries with ~zero mass dropped
        self._spawn_probs: tuple[tuple[int, float], ...] = tuple(
            (exp, prob) for exp, prob in self._spawn_model.exponent_probs if prob > 0.0
        )
        self._table: dict[tuple[int, int], float] = {}
        # per-choose search state
        self._nodes = 0
        self._deadline = math.inf
        self._node_cap = math.inf
        self._poll_armed = False

    @property
    def name(self) -> str:
        parts: list[str] = []
        if self._depth is not None:
            parts.append(f"depth={self._depth}")
        if self._budget_ms is not None:
            parts.append(f"budget={self._budget_ms:g}ms")
        if self._max_nodes is not None:
            parts.append(f"nodes={self._max_nodes}")
        return f"expectimax({','.join(parts)})"

    @property
    def depth(self) -> int | None:
        """Depth ceiling before the adaptive bonus (``None`` = unbounded)."""
        return self._depth

    @property
    def budget_ms(self) -> float | None:
        return self._budget_ms

    @property
    def max_nodes(self) -> int | None:
        return self._max_nodes

    def choose(self, board: Board) -> Decision:
        start = time.perf_counter()
        bb = _pack(board)
        empty_count = len(_empty_shifts(bb))
        cramped = self._adaptive and empty_count <= _ADAPTIVE_EMPTY_THRESHOLD
        max_depth: int | None = None
        if self._depth is not None:
            max_depth = self._depth + (1 if cramped else 0)

        limited = self._budget_ms is not None or self._max_nodes is not None
        self._nodes = 0
        self._deadline = (
            start + self._budget_ms / 1000.0 if self._budget_ms is not None else math.inf
        )
        self._node_cap = self._max_nodes if self._max_nodes is not None else math.inf
        # depth 1 always completes: the first iteration runs with polling disarmed
        self._poll_armed = False

        # Depth-only: one direct pass at the ceiling (today's search, bit for bit).
        # Any limit set: iterative deepening so a timeout has a completed answer.
        ceiling = max_depth if max_depth is not None else _DEEPENING_CEILING
        depth = ceiling if not limited else 1
        reached = 0
        best_dir: Direction | None = None
        move_values: dict[str, float] = {}
        stop = "depth"
        while True:
            try:
                best_dir, move_values = self._search_root(bb, depth)
            except _SearchAborted as aborted:
                stop = aborted.stop
                break
            reached = depth
            if depth >= ceiling:
                stop = "depth" if max_depth is not None else "ladder_end"
                break
            # between iterations: do not start a pass the limits already forbid
            if self._nodes >= self._node_cap:
                stop = "nodes"
                break
            if time.perf_counter() >= self._deadline:
                stop = "budget"
                break
            depth += 1
            self._poll_armed = True
        if best_dir is None:
            raise NoLegalMoveError("no legal moves available")

        eval_ms = (time.perf_counter() - start) * 1000.0
        details: dict[str, Any] = {
            "move_values": move_values,
            "heuristic": _breakdown_bb(bb, self._weights),
            "empty_cells": empty_count,
            "table_size": len(self._table),
            "search": {
                "stop": stop,
                "nodes": self._nodes,
                "max_depth": max_depth,
                "budget_ms": self._budget_ms,
                "max_nodes": self._max_nodes,
            },
        }
        return Decision(
            direction=best_dir,
            eval_ms=eval_ms,
            search_depth=reached,
            details=details,
        )

    # --- internal search ---------------------------------------------------

    def _search_root(self, bb: int, depth: int) -> tuple[Direction | None, dict[str, float]]:
        """One fixed-depth pass from a fresh table: ``(best move, value per move)``."""
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
        return best_dir, move_values

    def _poll(self) -> None:
        if not self._poll_armed:
            return
        if self._nodes >= self._node_cap:
            raise _SearchAborted("nodes")
        if time.perf_counter() >= self._deadline:
            raise _SearchAborted("budget")

    def _chance_value(self, bb: int, depth: int, probability: float) -> float:
        """Expected value over all spawns on ``bb`` (a post-move board)."""
        if depth <= 0 or probability < PRUNE_PROBABILITY:
            return _evaluate_bb(bb, self._weights)
        # One increment per interior chance node -- the Go engines' unit, so
        # ``nodes``/``max_nodes`` compare across engines. Doubles as the poll clock.
        self._nodes += 1
        if not self._nodes & _POLL_MASK:
            self._poll()
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

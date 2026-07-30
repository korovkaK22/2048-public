"""Board evaluation heuristics for 2048 strategies.

Value = weighted sum of:
    * empty        — number of empty cells (more space = more options),
    * monotonicity — snake-friendly: per row/column, penalty is the smaller
      of the "increasing" and "decreasing" violation sums over the packed
      (gap-free) tile sequence, so each line may be monotone in either
      direction (which is exactly what a snake layout needs),
    * smoothness   — negative sum of |exponent differences| between
      neighbouring tiles (gaps skipped) along rows and columns,
    * corner       — max exponent, awarded only while the largest tile
      sits in one of the four board corners.

All components are precomputed per 16-bit row at import time, so a full
board evaluation is just a handful of table lookups — cheap enough for
hundreds of thousands of expectimax leaf nodes per second.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from bot2048.core.engine import _pack, _transpose
from bot2048.core.types import Board

__all__ = ["DEFAULT_WEIGHTS", "HeuristicWeights", "breakdown", "evaluate"]


@dataclass(frozen=True, slots=True)
class HeuristicWeights:
    """Weights for the evaluation components. Defaults are battle-tested-ish."""

    empty: float = 200.0
    monotonicity: float = 120.0
    smoothness: float = 25.0
    corner: float = 1100.0


DEFAULT_WEIGHTS = HeuristicWeights()

_ROW_MASK = 0xFFFF
_ROW_SHIFTS = (0, 16, 32, 48)
_CORNER_SHIFTS = (0, 12, 48, 60)  # cells 0, 3, 12, 15


def _build_tables() -> tuple[list[int], list[int], list[int], list[int]]:
    empty = [0] * 65536
    mono = [0] * 65536
    smooth = [0] * 65536
    max_exp = [0] * 65536
    for row in range(65536):
        cells = (row & 0xF, (row >> 4) & 0xF, (row >> 8) & 0xF, (row >> 12) & 0xF)
        empty[row] = sum(1 for c in cells if c == 0)
        max_exp[row] = max(cells)
        tiles = [c for c in cells if c]
        inc = dec = rough = 0
        for a, b in pairwise(tiles):
            if b > a:
                inc += b - a
            else:
                dec += a - b
            rough += abs(a - b)
        mono[row] = -min(inc, dec)
        smooth[row] = -rough
    return empty, mono, smooth, max_exp


_ROW_EMPTY, _ROW_MONO, _ROW_SMOOTH, _ROW_MAXEXP = _build_tables()


def _components_bb(bb: int) -> tuple[int, int, int, int]:
    """(empty, monotonicity, smoothness, corner) raw components of a bitboard."""
    transposed = _transpose(bb)
    empty = mono = smooth = 0
    max_exp = 0
    for shift in _ROW_SHIFTS:
        row = (bb >> shift) & _ROW_MASK
        col = (transposed >> shift) & _ROW_MASK
        empty += _ROW_EMPTY[row]
        mono += _ROW_MONO[row] + _ROW_MONO[col]
        smooth += _ROW_SMOOTH[row] + _ROW_SMOOTH[col]
        if _ROW_MAXEXP[row] > max_exp:
            max_exp = _ROW_MAXEXP[row]
    corner = 0
    if max_exp and any((bb >> s) & 0xF == max_exp for s in _CORNER_SHIFTS):
        corner = max_exp
    return empty, mono, smooth, corner


def _evaluate_bb(bb: int, weights: HeuristicWeights) -> float:
    """Fast path used by search: evaluate a bitboard directly."""
    empty, mono, smooth, corner = _components_bb(bb)
    return (
        weights.empty * empty
        + weights.monotonicity * mono
        + weights.smoothness * smooth
        + weights.corner * corner
    )


def _breakdown_bb(bb: int, weights: HeuristicWeights) -> dict[str, float]:
    empty, mono, smooth, corner = _components_bb(bb)
    parts = {
        "empty": weights.empty * empty,
        "monotonicity": weights.monotonicity * mono,
        "smoothness": weights.smoothness * smooth,
        "corner": weights.corner * corner,
    }
    parts["total"] = sum(parts.values())
    return parts


def evaluate(board: Board, weights: HeuristicWeights = DEFAULT_WEIGHTS) -> float:
    """Weighted heuristic value of a public board."""
    return _evaluate_bb(_pack(board), weights)


def breakdown(board: Board, weights: HeuristicWeights = DEFAULT_WEIGHTS) -> dict[str, float]:
    """Per-component weighted contributions plus ``total``."""
    return _breakdown_bb(_pack(board), weights)

"""Core domain types shared across ALL bot2048 modules.

This file is the contract between core engine, AI, simulation, storage,
vision and device layers. Changes here must be coordinated.

Board representation (public API):
    A tuple of 16 tile EXPONENTS in row-major order (index = row * 4 + col).
    0 means empty cell; k (k >= 1) means tile with value 2**k.
    Tuples are immutable and hashable -> safe as dict/cache keys.

The engine is free to use a faster internal representation (e.g. a 64-bit
bitboard with 4 bits per cell) but must expose this public type.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TypeAlias

Board: TypeAlias = tuple[int, ...]

BOARD_SIZE: int = 4
BOARD_CELLS: int = BOARD_SIZE * BOARD_SIZE
EMPTY_BOARD: Board = (0,) * BOARD_CELLS


class Direction(IntEnum):
    """Swipe / move direction. Values are stable — stored in the DB."""

    UP = 0
    RIGHT = 1
    DOWN = 2
    LEFT = 3


@dataclass(frozen=True, slots=True)
class MoveResult:
    """Result of applying a move WITHOUT the subsequent random spawn."""

    board: Board
    score_gained: int
    moved: bool  # False -> illegal move (nothing slid or merged)


@dataclass(frozen=True, slots=True)
class Spawn:
    """A single random tile spawn."""

    cell: int  # 0..15, row-major index
    exponent: int  # 1 -> tile 2, 2 -> tile 4, ...


@dataclass(frozen=True, slots=True)
class SpawnModel:
    """Probability model for tile spawns.

    Default = classic 2048: 90% tile 2, 10% tile 4, uniform over empty cells.
    The live "research mode" estimates this empirically for the monobank
    clone and may replace the default.
    """

    exponent_probs: tuple[tuple[int, float], ...] = ((1, 0.9), (2, 0.1))
    initial_tiles: int = 2  # tiles spawned on a fresh board

    def __post_init__(self) -> None:
        total = sum(p for _, p in self.exponent_probs)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"spawn probabilities must sum to 1.0, got {total}")


def board_values(board: Board) -> tuple[int, ...]:
    """Exponents -> human-readable tile values (0 stays 0)."""
    return tuple(0 if e == 0 else 2**e for e in board)


def board_from_values(values: tuple[int, ...]) -> Board:
    """Human-readable tile values -> exponents. Validates powers of two."""
    out: list[int] = []
    for v in values:
        if v == 0:
            out.append(0)
            continue
        e = v.bit_length() - 1
        if 2**e != v:
            raise ValueError(f"{v} is not a power of two")
        out.append(e)
    return tuple(out)

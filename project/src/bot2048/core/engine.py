"""Pure, deterministic 2048 game engine.

NO I/O, NO logging, NO global mutable state. The public board type is
``Board`` (tuple of 16 exponents, row-major) from :mod:`bot2048.core.types`.

Internally boards are packed into a 64-bit bitboard (4 bits per cell,
cell ``i`` occupies bits ``4*i .. 4*i+3``). All row moves are resolved via
tables precomputed once at import time for every possible 16-bit row.

Merge rules (classic 2048):
    * tiles slide toward the move direction,
    * each pair merges at most once per move,
    * merges resolve from the leading edge,
    * score gained = sum of the values of the tiles created by merges.

Cell exponents are capped at 15 (tile 32768) by the 4-bit representation;
two exponent-15 tiles do not merge further.
"""

from __future__ import annotations

import random

from bot2048.core.types import (
    BOARD_CELLS,
    EMPTY_BOARD,
    Board,
    Direction,
    MoveResult,
    Spawn,
    SpawnModel,
)

__all__ = [
    "EngineError",
    "NoEmptyCellsError",
    "empty_cells",
    "is_game_over",
    "legal_moves",
    "max_exponent",
    "move",
    "new_game",
    "spawn_random",
]

MAX_CELL_EXPONENT: int = 15


class EngineError(Exception):
    """Base class for engine errors."""


class NoEmptyCellsError(EngineError):
    """Raised when a tile spawn is requested on a board without empty cells."""


# --------------------------------------------------------------------------
# Bitboard helpers (private)
# --------------------------------------------------------------------------

_SHIFTS: tuple[int, ...] = tuple(4 * i for i in range(BOARD_CELLS))
_ROW_SHIFTS: tuple[int, ...] = (0, 16, 32, 48)
_ROW_MASK: int = 0xFFFF


def _pack(board: Board) -> int:
    """Board tuple -> 64-bit bitboard. Validates cell range and length."""
    bb = 0
    for shift, exp in zip(_SHIFTS, board, strict=True):
        if not 0 <= exp <= MAX_CELL_EXPONENT:
            raise EngineError(f"cell exponent {exp} out of range 0..{MAX_CELL_EXPONENT}")
        bb |= exp << shift
    return bb


def _unpack(bb: int) -> Board:
    """64-bit bitboard -> board tuple."""
    return tuple((bb >> shift) & 0xF for shift in _SHIFTS)


def _transpose(bb: int) -> int:
    """Transpose the 4x4 nibble matrix of a bitboard (rows <-> columns)."""
    a1 = bb & 0xF0F00F0FF0F00F0F
    a2 = bb & 0x0000F0F00000F0F0
    a3 = bb & 0x0F0F00000F0F0000
    a = a1 | (a2 << 12) | (a3 >> 12)
    b1 = a & 0xFF00FF0000FF00FF
    b2 = a & 0x00FF00FF00000000
    b3 = a & 0x00000000FF00FF00
    return b1 | (b2 >> 24) | (b3 << 24)


def _reverse_row(row: int) -> int:
    """Reverse the 4 nibbles of a 16-bit row."""
    return (
        ((row >> 12) & 0x000F)
        | ((row >> 4) & 0x00F0)
        | ((row << 4) & 0x0F00)
        | ((row << 12) & 0xF000)
    )


def _merge_row_left(cells: list[int]) -> tuple[list[int], int]:
    """Reference merge of one row toward index 0. Used only to build tables."""
    tiles = [c for c in cells if c]
    out: list[int] = []
    score = 0
    i = 0
    while i < len(tiles):
        if i + 1 < len(tiles) and tiles[i] == tiles[i + 1] and tiles[i] < MAX_CELL_EXPONENT:
            out.append(tiles[i] + 1)
            score += 1 << (tiles[i] + 1)
            i += 2
        else:
            out.append(tiles[i])
            i += 1
    out.extend([0] * (4 - len(out)))
    return out, score


def _build_row_tables() -> tuple[list[int], list[int], list[int], list[int]]:
    left = [0] * 65536
    left_score = [0] * 65536
    right = [0] * 65536
    right_score = [0] * 65536
    for row in range(65536):
        cells = [row & 0xF, (row >> 4) & 0xF, (row >> 8) & 0xF, (row >> 12) & 0xF]
        merged, score = _merge_row_left(cells)
        left[row] = merged[0] | (merged[1] << 4) | (merged[2] << 8) | (merged[3] << 12)
        left_score[row] = score
    for row in range(65536):
        rev = _reverse_row(row)
        right[row] = _reverse_row(left[rev])
        right_score[row] = left_score[rev]
    return left, left_score, right, right_score


_ROW_LEFT, _ROW_LEFT_SCORE, _ROW_RIGHT, _ROW_RIGHT_SCORE = _build_row_tables()


def _move_rows(bb: int, table: list[int], scores: list[int]) -> tuple[int, int]:
    r0 = bb & _ROW_MASK
    r1 = (bb >> 16) & _ROW_MASK
    r2 = (bb >> 32) & _ROW_MASK
    r3 = (bb >> 48) & _ROW_MASK
    new_bb = table[r0] | (table[r1] << 16) | (table[r2] << 32) | (table[r3] << 48)
    return new_bb, scores[r0] + scores[r1] + scores[r2] + scores[r3]


def _move_bb(bb: int, direction: Direction) -> tuple[int, int]:
    """Apply a move on a bitboard. Returns (new_bitboard, score_gained)."""
    if direction is Direction.LEFT:
        return _move_rows(bb, _ROW_LEFT, _ROW_LEFT_SCORE)
    if direction is Direction.RIGHT:
        return _move_rows(bb, _ROW_RIGHT, _ROW_RIGHT_SCORE)
    if direction is Direction.UP:
        new_bb, score = _move_rows(_transpose(bb), _ROW_LEFT, _ROW_LEFT_SCORE)
        return _transpose(new_bb), score
    new_bb, score = _move_rows(_transpose(bb), _ROW_RIGHT, _ROW_RIGHT_SCORE)
    return _transpose(new_bb), score


# --------------------------------------------------------------------------
# Public API (per docs/architecture.md)
# --------------------------------------------------------------------------


def move(board: Board, direction: Direction) -> MoveResult:
    """Apply a move. An illegal move returns the original board, moved=False."""
    bb = _pack(board)
    new_bb, score = _move_bb(bb, direction)
    if new_bb == bb:
        return MoveResult(board=board, score_gained=0, moved=False)
    return MoveResult(board=_unpack(new_bb), score_gained=score, moved=True)


def legal_moves(board: Board) -> list[Direction]:
    """Directions that change the board, in ``Direction`` enum order."""
    bb = _pack(board)
    return [d for d in Direction if _move_bb(bb, d)[0] != bb]


def is_game_over(board: Board) -> bool:
    """True when no move changes the board."""
    return not legal_moves(board)


def empty_cells(board: Board) -> tuple[int, ...]:
    """Row-major indices of empty cells."""
    return tuple(i for i, exp in enumerate(board) if exp == 0)


def max_exponent(board: Board) -> int:
    """Largest exponent on the board (0 for an empty board)."""
    return max(board)


def spawn_random(
    board: Board, model: SpawnModel, rng: random.Random
) -> tuple[Board, Spawn]:
    """Spawn one tile: uniform over empty cells, exponent per ``model``.

    Consumes exactly two rng values (cell pick, exponent roll) so the
    sequence is stable and reproducible for a given seed.
    """
    empties = empty_cells(board)
    if not empties:
        raise NoEmptyCellsError("cannot spawn a tile: board has no empty cells")
    cell = empties[rng.randrange(len(empties))]
    roll = rng.random()
    cumulative = 0.0
    exponent = model.exponent_probs[-1][0]
    for exp, prob in model.exponent_probs:
        cumulative += prob
        if roll < cumulative:
            exponent = exp
            break
    cells = list(board)
    cells[cell] = exponent
    return tuple(cells), Spawn(cell=cell, exponent=exponent)


def new_game(model: SpawnModel, rng: random.Random) -> tuple[Board, list[Spawn]]:
    """Fresh board with ``model.initial_tiles`` random tiles."""
    board: Board = EMPTY_BOARD
    spawns: list[Spawn] = []
    for _ in range(model.initial_tiles):
        board, spawn = spawn_random(board, model, rng)
        spawns.append(spawn)
    return board, spawns

"""ASCII rendering of boards from stored telemetry.

Deliberately self-contained: replay must work from the database alone, so
this module never imports the game engine or AI — only core types and the
storage hex codec.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from bot2048.core.types import BOARD_CELLS, BOARD_SIZE, Board, Direction

_CELL_WIDTH: Final[int] = 6  # fits "32768" (max storable tile) + padding

DIRECTION_GLYPHS: Final[Mapping[Direction, str]] = {
    Direction.UP: "^",
    Direction.RIGHT: ">",
    Direction.DOWN: "v",
    Direction.LEFT: "<",
}


def render_board(board: Board) -> str:
    """Render a board as an ASCII grid of tile values (empty cells blank)."""
    if len(board) != BOARD_CELLS:
        raise ValueError(f"board must have {BOARD_CELLS} cells, got {len(board)}")
    border = "+" + "+".join(["-" * _CELL_WIDTH] * BOARD_SIZE) + "+"
    lines = [border]
    for row in range(BOARD_SIZE):
        cells: list[str] = []
        for col in range(BOARD_SIZE):
            exponent = board[row * BOARD_SIZE + col]
            label = "" if exponent == 0 else str(2**exponent)
            cells.append(label.rjust(_CELL_WIDTH - 1) + " ")
        lines.append("|" + "|".join(cells) + "|")
        lines.append(border)
    return "\n".join(lines)


def render_hex_board(hex_board: str) -> str:
    """Render a board straight from its stored 16-char hex encoding."""
    from bot2048.storage.schema import board_from_hex

    return render_board(board_from_hex(hex_board))

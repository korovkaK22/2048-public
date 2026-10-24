"""Game-end notification text for the live loop (docs/telegram-notify.md).

Extracted VERBATIM from :mod:`bot2048.runner.live`: the pure, locale-free
formatting helpers behind the rich game-over / win notification --
:func:`format_duration`, :func:`_render_final_grid`,
:func:`_format_game_end_notify` -- and the :data:`FARM_SIM_MEAN` baseline the
score delta is measured against. Nothing here touches ``self``, the device or
the database: :meth:`~bot2048.runner.live.LiveRunner._end_game` computes the
inputs (board stats, duration, DB percentile) and hands them in. Additive by
design -- nothing composed here is written to the database; it only enriches
the human-facing message.

:mod:`bot2048.runner.live` re-exports every name so
``from bot2048.runner.live import format_duration`` keeps working for tests and
downstream callers.
"""

from __future__ import annotations

from bot2048.core.types import BOARD_SIZE, Board
from bot2048.runner.move_telemetry import _BoardStats
from bot2048.storage.history import FarmPercentile

__all__ = [
    "FARM_SIM_MEAN",
    "format_duration",
]

#: Farm-strategy simulation baseline mean score (~28.6k, docs/experiment-log.md).
#: The game-end notification shows this game's score as +/- vs this constant so a
#: single game is instantly comparable to the strategy's expected output even
#: when the DB percentile is unavailable (fresh DB / too few games).
FARM_SIM_MEAN: int = 28600


def format_duration(seconds: float) -> str:
    """Human clock string: ``594 -> '9:54'``; an hour or more -> ``'H:MM:SS'``.

    Rounds to the nearest whole second; negatives clamp to ``'0:00'``. Pure and
    locale-free, so it is trivially testable and identical everywhere.
    """
    total = int(max(seconds, 0.0) + 0.5)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _render_final_grid(board: Board) -> str:
    """4x4 monospace grid of tile VALUES (``.`` = empty), for a TG code block.

    Renders positions -- not just a count -- so the notification shows WHERE the
    tiles jammed. Columns are right-aligned to the widest present value.
    """
    values = [0 if exponent == 0 else 1 << exponent for exponent in board]
    width = max((len(str(v)) for v in values if v), default=1)
    lines: list[str] = []
    for row in range(BOARD_SIZE):
        cells = [
            ("." if values[row * BOARD_SIZE + col] == 0 else str(values[row * BOARD_SIZE + col]))
            .rjust(width)
            for col in range(BOARD_SIZE)
        ]
        lines.append(" ".join(cells))
    return "\n".join(lines)


def _format_game_end_notify(
    *,
    game_id: str,
    end_reason: str,
    score: int,
    moves: int,
    duration_s: float,
    stats: _BoardStats,
    board: Board,
    percentile: FarmPercentile | None,
) -> str:
    """Compose the rich game-over/win notification body (docs/telegram-notify.md).

    Layout: header, one-line summary, a fenced 4x4 board grid (rendered as a
    monospace Markdown code block by :func:`bot2048.tgnotify._maybe_markdown`),
    the tile breakdown, the
    fill/sum line, the farm-baseline delta, and -- when available -- the DB
    percentile among past farm games. Additive: nothing here is written to the
    database; it only enriches the human-facing message.
    """
    per_move = round(score / moves) if moves > 0 else 0
    tiles = ", ".join(f"{value}×{count}" for value, count in stats.histogram.items()) or "—"
    delta = score - FARM_SIM_MEAN
    delta_pct = round(100 * delta / FARM_SIM_MEAN) if FARM_SIM_MEAN else 0
    sign = "+" if delta >= 0 else ""
    lines = [
        f"🏁 Гра {game_id[:8]} — {end_reason}",
        f"Рахунок {score} · {moves} ходів · {format_duration(duration_s)}"
        f" · {per_move}/хід · макс. плитка {stats.highest}",
        "```\n" + _render_final_grid(board) + "\n```",
        f"Плитки: {tiles}",
        f"Порожніх: {stats.empty} · сума {stats.total}",
        f"vs farm-базлайн {FARM_SIM_MEAN / 1000:.1f}k: {sign}{delta} ({sign}{delta_pct}%)",
    ]
    if percentile is not None:
        lines.append(
            f"Краще за {percentile.better_pct:.0f}% минулих farm-ігор "
            f"(N={percentile.sample_size})"
        )
    return "\n".join(lines)

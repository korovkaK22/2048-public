"""Read-only historical score statistics for game-end notifications.

Best-effort, strictly ``mode=ro``: given the telemetry database and a fresh
final score, :func:`farm_score_percentile` reports where that score lands among
the scores of PAST naturally-ended farm games (``end_reason`` in
:data:`COMPLETED_END_REASONS`). It never writes, never raises for the caller
(bad/missing DB, locked file, too few samples -> ``None``) and never touches
the frozen recording/storage contracts.

"Better than X%" is defined as the fraction of past completed games whose
``final_score`` is STRICTLY LESS than this score, times 100. The current game
is excluded (``exclude_game_id``) so a game is never compared against itself,
and aborted/errored games (artificially low scores) are excluded from the
baseline population so the percentile reflects real end-of-game outcomes only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from bot2048.storage.latency import open_readonly

log = structlog.get_logger(__name__)

#: End reasons that count as a genuine end-of-game outcome (the baseline
#: population for the percentile). Aborted / interrupted / errored / rejected
#: games carry artificially low scores and would deflate the baseline.
COMPLETED_END_REASONS: tuple[str, ...] = ("game_over", "win_2048")

#: Below this many past completed games the percentile is statistically
#: meaningless and the notification simply omits the line.
DEFAULT_MIN_GAMES: int = 5


@dataclass(frozen=True, slots=True)
class FarmPercentile:
    """Where a score lands among past completed farm games.

    ``better_pct`` is the percentage of the ``sample_size`` past games this
    score strictly beats; ``sample_size`` is that comparison population size.
    """

    better_pct: float
    sample_size: int


def farm_score_percentile(
    db_path: str | Path,
    score: int,
    *,
    exclude_game_id: str | None = None,
    min_games: int = DEFAULT_MIN_GAMES,
) -> FarmPercentile | None:
    """Percentile of ``score`` among past completed games, or ``None``.

    Returns ``None`` (never raises) when the database is missing/unreadable or
    fewer than ``min_games`` past completed games exist. Read-only.
    """
    placeholders = ",".join("?" for _ in COMPLETED_END_REASONS)
    sql = (
        "SELECT "
        "  SUM(CASE WHEN final_score < ? THEN 1 ELSE 0 END), "
        "  COUNT(*) "
        "FROM games "
        "WHERE final_score IS NOT NULL "
        f"  AND end_reason IN ({placeholders}) "
        "  AND (? IS NULL OR id != ?)"
    )
    params: list[object] = [score, *COMPLETED_END_REASONS, exclude_game_id, exclude_game_id]
    try:
        conn = open_readonly(db_path)
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # best-effort telemetry: any failure -> skip the line
        log.debug("history.percentile_unavailable", error=type(exc).__name__)
        return None

    if row is None:
        return None
    below, total = row[0], row[1]
    if not total or total < min_games:
        return None
    better_pct = 100.0 * (below or 0) / total
    return FarmPercentile(better_pct=better_pct, sample_size=int(total))

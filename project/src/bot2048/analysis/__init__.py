"""Post-hoc analysis of recorded telemetry: stats, chi-square, ASCII rendering."""

from bot2048.analysis.render import DIRECTION_GLYPHS, render_board, render_hex_board
from bot2048.analysis.stats import (
    CHI2_CRIT_P01,
    CLASSIC_SPAWN_PROBS,
    ChiSquareResult,
    ScoreStats,
    chi_square_vs_model,
    max_tile_histogram,
    score_stats,
)

__all__ = [
    "CHI2_CRIT_P01",
    "CLASSIC_SPAWN_PROBS",
    "DIRECTION_GLYPHS",
    "ChiSquareResult",
    "ScoreStats",
    "chi_square_vs_model",
    "max_tile_histogram",
    "render_board",
    "render_hex_board",
    "score_stats",
]

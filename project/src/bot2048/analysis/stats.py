"""Aggregate statistics over recorded games and spawns.

Pure functions over repository read models — no I/O, no scipy. The
chi-square helper is a plain computed statistic used to compare the
empirical spawn distribution of the monobank clone against classic 90/10.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean, median
from typing import Final

from bot2048.core.types import SpawnModel
from bot2048.storage.repository import GameSummary

CLASSIC_SPAWN_PROBS: Final[tuple[tuple[int, float], ...]] = SpawnModel().exponent_probs
"""Classic 2048 spawn model: 90% tile 2 (exponent 1), 10% tile 4 (exponent 2)."""

CHI2_CRIT_P01: Final[Mapping[int, float]] = {
    1: 6.635,
    2: 9.210,
    3: 11.345,
    4: 13.277,
    5: 15.086,
}
"""Chi-square critical values at p=0.01 for small degrees of freedom."""


@dataclass(frozen=True, slots=True)
class ScoreStats:
    """Score/move aggregates over the *finished* games of one experiment."""

    games: int  # all games seen (incl. unfinished)
    finished: int  # games with a recorded GameEnded
    mean_score: float
    median_score: float
    max_score: int
    avg_moves: float


@dataclass(frozen=True, slots=True)
class ChiSquareResult:
    """Pearson chi-square statistic of observed counts vs a model."""

    statistic: float  # math.inf if a category with model probability 0 was observed
    degrees_of_freedom: int
    total: int


def score_stats(summaries: Sequence[GameSummary]) -> ScoreStats | None:
    """Aggregate finished-game stats; None if no game has finished yet."""
    scores: list[int] = []
    moves: list[int] = []
    for summary in summaries:
        if summary.final_score is not None:
            scores.append(summary.final_score)
        if summary.num_moves is not None:
            moves.append(summary.num_moves)
    if not scores:
        return None
    return ScoreStats(
        games=len(summaries),
        finished=len(scores),
        mean_score=fmean(scores),
        median_score=float(median(scores)),
        max_score=max(scores),
        avg_moves=fmean(moves) if moves else 0.0,
    )


def max_tile_histogram(summaries: Sequence[GameSummary]) -> dict[int, int]:
    """Count finished games by max exponent reached: {exponent: games}."""
    histogram: dict[int, int] = {}
    for summary in summaries:
        if summary.max_exponent is not None:
            histogram[summary.max_exponent] = histogram.get(summary.max_exponent, 0) + 1
    return histogram


def chi_square_vs_model(
    observed: Mapping[int, int],
    model: Sequence[tuple[int, float]] = CLASSIC_SPAWN_PROBS,
) -> ChiSquareResult | None:
    """Chi-square of observed spawn-exponent counts against a spawn model.

    Categories present in either the observations or the model are compared;
    observing a category the model gives probability 0 yields an infinite
    statistic (the model is certainly wrong). Returns None for no data.
    """
    total = sum(observed.values())
    if total == 0:
        return None
    expected = {exponent: prob * total for exponent, prob in model}
    categories = sorted(set(observed) | set(expected))
    statistic = 0.0
    for category in categories:
        obs = observed.get(category, 0)
        exp = expected.get(category, 0.0)
        if exp <= 0.0:
            if obs > 0:
                statistic = math.inf
            continue
        statistic += (obs - exp) ** 2 / exp
    return ChiSquareResult(
        statistic=statistic,
        degrees_of_freedom=max(len(categories) - 1, 1),
        total=total,
    )

"""AI strategies for the 2048 bot."""

from bot2048.ai.expectimax import ExpectimaxStrategy
from bot2048.ai.heuristics import DEFAULT_WEIGHTS, HeuristicWeights, breakdown, evaluate
from bot2048.ai.strategies import (
    CornerStrategy,
    Decision,
    GreedyStrategy,
    NoLegalMoveError,
    RandomStrategy,
    Strategy,
)

__all__ = [
    "DEFAULT_WEIGHTS",
    "CornerStrategy",
    "Decision",
    "ExpectimaxStrategy",
    "GreedyStrategy",
    "HeuristicWeights",
    "NoLegalMoveError",
    "RandomStrategy",
    "Strategy",
    "breakdown",
    "evaluate",
]

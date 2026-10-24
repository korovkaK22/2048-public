"""AI strategies for the 2048 bot."""

from bot2048.ai.expectimax import ExpectimaxStrategy
from bot2048.ai.heuristics import DEFAULT_WEIGHTS, HeuristicWeights, breakdown, evaluate
from bot2048.ai.registry import list_specs, load_spec
from bot2048.ai.spec import (
    EngineSpec,
    Guard,
    LadderSpec,
    Limits,
    SearchSpec,
    SpecError,
    StrategySpec,
)
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
    "EngineSpec",
    "ExpectimaxStrategy",
    "GreedyStrategy",
    "Guard",
    "HeuristicWeights",
    "LadderSpec",
    "Limits",
    "NoLegalMoveError",
    "RandomStrategy",
    "SearchSpec",
    "SpecError",
    "Strategy",
    "StrategySpec",
    "breakdown",
    "evaluate",
    "list_specs",
    "load_spec",
]

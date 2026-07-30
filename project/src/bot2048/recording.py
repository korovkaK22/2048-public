"""Game telemetry contract: every game, move and spawn is recorded.

This is the interface between the game producers (simulator, live runner)
and the storage layer. Producers emit events; a Recorder persists them.
The storage module implements SqliteRecorder; tests may use an in-memory
ListRecorder.

Every played game — simulated or live — MUST be fully reconstructable
from its recorded events alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from bot2048.core.types import Board, Direction, Spawn


@dataclass(frozen=True, slots=True)
class GameStarted:
    game_id: str  # uuid4 hex
    experiment_id: str  # groups games of one run/benchmark
    kind: str  # "sim" | "live"
    strategy: str  # e.g. "expectimax(depth=4)"
    seed: int | None  # None for live games
    initial_board: Board
    params: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0  # unix time


@dataclass(frozen=True, slots=True)
class MovePlayed:
    game_id: str
    ply: int  # 0-based move counter
    direction: Direction
    board_before: Board
    board_after: Board  # after slide/merge, BEFORE spawn
    score_gained: int
    score_total: int
    eval_ms: float  # decision time
    search_depth: int | None = None
    eval_details: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0


@dataclass(frozen=True, slots=True)
class TileSpawned:
    game_id: str
    ply: int  # same ply as the move that preceded it; -1/-2 for initial tiles
    spawn: Spawn
    board_after: Board
    ts: float = 0.0


@dataclass(frozen=True, slots=True)
class GameEnded:
    game_id: str
    final_score: int
    max_exponent: int  # max tile = 2**max_exponent
    num_moves: int
    end_reason: str  # "game_over" | "aborted" | "error" | "target_reached"
    duration_s: float
    ts: float = 0.0


GameEvent = GameStarted | MovePlayed | TileSpawned | GameEnded


@runtime_checkable
class Recorder(Protocol):
    """Persists game events. Implementations must be safe to call once per
    move at high frequency (batch internally; flush at least on GameEnded)."""

    def record(self, event: GameEvent) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class ListRecorder:
    """In-memory recorder for tests and quick experiments."""

    def __init__(self) -> None:
        self.events: list[GameEvent] = []

    def record(self, event: GameEvent) -> None:
        self.events.append(event)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

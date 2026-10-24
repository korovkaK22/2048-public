"""Telemetry persistence: SQLite recorder (write) + repository (read)."""

from bot2048.storage.async_recorder import AsyncRecorder, RecorderStats
from bot2048.storage.errors import GameNotFoundError, RecorderError, StorageError
from bot2048.storage.repository import (
    ExperimentRow,
    GameRecord,
    GameRepository,
    GameSummary,
    MoveRow,
    SpawnRow,
    SpawnStats,
)
from bot2048.storage.schema import board_from_hex, board_to_hex
from bot2048.storage.sqlite_recorder import ExperimentMeta, SqliteRecorder

__all__ = [
    "AsyncRecorder",
    "ExperimentMeta",
    "ExperimentRow",
    "GameNotFoundError",
    "GameRecord",
    "GameRepository",
    "GameSummary",
    "MoveRow",
    "RecorderError",
    "RecorderStats",
    "SpawnRow",
    "SpawnStats",
    "SqliteRecorder",
    "StorageError",
    "board_from_hex",
    "board_to_hex",
]

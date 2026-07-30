"""Storage-layer exceptions.

All errors raised by the storage package derive from StorageError and carry
enough context (db path, game id, buffered counts) to debug post-mortem.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base error for the bot2048 storage layer."""


class RecorderError(StorageError):
    """SqliteRecorder failed to open the database, buffer or flush events."""


class GameNotFoundError(StorageError):
    """The requested game id is not present in the database."""

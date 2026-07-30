"""SqliteRecorder — the write side of the telemetry store.

Single-writer by design: exactly one SqliteRecorder writes to a given
database file at a time. SQLite WAL mode still allows concurrent readers
(e.g. ``bot2048 db report`` against a live run).

Events are buffered in memory and written in batches via ``executemany``.
The buffer is flushed at the latest on every GameEnded, on explicit
``flush()``/``close()``, and whenever more than ``batch_size`` events are
pending.

Note: ``GameStarted.initial_board`` has no dedicated column in the fixed
schema; it is fully reconstructable from the initial TileSpawned events
(ply -1/-2) or from ``moves[0].board_before``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import structlog
from structlog.stdlib import get_logger

from bot2048.recording import GameEnded, GameEvent, GameStarted, MovePlayed, TileSpawned
from bot2048.storage.errors import RecorderError
from bot2048.storage.schema import board_to_hex, connect, ensure_schema

log = get_logger(__name__)


def _log_debug(event: str, **kw: object) -> None:
    """Debug-log only when the application configured structlog.

    Unconfigured structlog prints straight to stdout; a library must stay
    silent unless ``bot2048.logging_setup.setup_logging`` (or equivalent)
    has been called.
    """
    if structlog.is_configured():
        log.debug(event, **kw)


@dataclass(frozen=True, slots=True)
class ExperimentMeta:
    """Metadata describing one experiment (a run/benchmark grouping games)."""

    experiment_id: str
    kind: str  # "sim" | "live"
    strategy: str
    params: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


def _dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


_INSERT_EXPERIMENT = (
    "INSERT OR IGNORE INTO experiments (id, created_at, kind, strategy, params_json, notes) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_INSERT_GAME = "INSERT INTO games (id, experiment_id, seed, started_at) VALUES (?, ?, ?, ?)"
_UPDATE_GAME_END = (
    "UPDATE games SET ended_at = ?, final_score = ?, max_exponent = ?, num_moves = ?, "
    "end_reason = ?, duration_s = ? WHERE id = ?"
)
_INSERT_MOVE = (
    "INSERT INTO moves (game_id, ply, direction, board_before, board_after, score_gained, "
    "score_total, eval_ms, search_depth, eval_details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_SPAWN = (
    "INSERT INTO spawns (game_id, ply, cell, exponent, board_after) VALUES (?, ?, ?, ?, ?)"
)


class SqliteRecorder:
    """Recorder protocol implementation persisting events to SQLite.

    Usable as a context manager; ``close()`` is idempotent and flushes any
    buffered events first. Recording after close raises RecorderError.
    """

    def __init__(
        self,
        db_path: str | Path,
        experiment: ExperimentMeta,
        *,
        batch_size: int = 256,
    ) -> None:
        self._db_path = Path(db_path)
        self._experiment = experiment
        self._batch_size = max(1, batch_size)
        self._closed = False
        self._pending = 0
        self._exp_rows: list[tuple[str, float, str, str, str, str]] = []
        self._game_rows: list[tuple[str, str, int | None, float]] = []
        self._move_rows: list[tuple[str, int, int, str, str, int, int, float, int | None, str]] = []
        self._spawn_rows: list[tuple[str, int, int, int, str]] = []
        self._end_rows: list[tuple[float, int, int, int, str, float, str]] = []
        self._known_experiments: set[str] = {experiment.experiment_id}
        try:
            self._conn = connect(self._db_path)
            ensure_schema(self._conn)
            with self._conn:
                self._conn.execute(
                    _INSERT_EXPERIMENT,
                    (
                        experiment.experiment_id,
                        time.time(),
                        experiment.kind,
                        experiment.strategy,
                        _dumps(experiment.params),
                        experiment.notes,
                    ),
                )
        except sqlite3.Error as exc:
            raise RecorderError(f"failed to open telemetry db {self._db_path}: {exc}") from exc
        _log_debug(
            "recorder_opened", db=str(self._db_path), experiment_id=experiment.experiment_id
        )

    def record(self, event: GameEvent) -> None:
        """Buffer one event; may trigger a batched write."""
        if self._closed:
            raise RecorderError(f"recorder for {self._db_path} is closed")
        match event:
            case GameStarted():
                self._on_game_started(event)
            case MovePlayed():
                self._move_rows.append(
                    (
                        event.game_id,
                        event.ply,
                        int(event.direction),
                        board_to_hex(event.board_before),
                        board_to_hex(event.board_after),
                        event.score_gained,
                        event.score_total,
                        event.eval_ms,
                        event.search_depth,
                        _dumps(event.eval_details),
                    )
                )
            case TileSpawned():
                self._spawn_rows.append(
                    (
                        event.game_id,
                        event.ply,
                        event.spawn.cell,
                        event.spawn.exponent,
                        board_to_hex(event.board_after),
                    )
                )
            case GameEnded():
                self._end_rows.append(
                    (
                        event.ts if event.ts else time.time(),
                        event.final_score,
                        event.max_exponent,
                        event.num_moves,
                        event.end_reason,
                        event.duration_s,
                        event.game_id,
                    )
                )
                self._pending += 1
                self.flush()
                return
        self._pending += 1
        if self._pending >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """Write all buffered events in one transaction (no-op if empty/closed)."""
        if self._closed or self._pending == 0:
            return
        counts = (
            len(self._exp_rows),
            len(self._game_rows),
            len(self._move_rows),
            len(self._spawn_rows),
            len(self._end_rows),
        )
        try:
            with self._conn:
                if self._exp_rows:
                    self._conn.executemany(_INSERT_EXPERIMENT, self._exp_rows)
                if self._game_rows:
                    self._conn.executemany(_INSERT_GAME, self._game_rows)
                if self._move_rows:
                    self._conn.executemany(_INSERT_MOVE, self._move_rows)
                if self._spawn_rows:
                    self._conn.executemany(_INSERT_SPAWN, self._spawn_rows)
                if self._end_rows:
                    self._conn.executemany(_UPDATE_GAME_END, self._end_rows)
        except sqlite3.Error as exc:
            raise RecorderError(
                f"flush to {self._db_path} failed (experiments={counts[0]}, games={counts[1]}, "
                f"moves={counts[2]}, spawns={counts[3]}, game_ends={counts[4]}): {exc}"
            ) from exc
        self._exp_rows.clear()
        self._game_rows.clear()
        self._move_rows.clear()
        self._spawn_rows.clear()
        self._end_rows.clear()
        self._pending = 0
        _log_debug(
            "telemetry_flushed",
            db=str(self._db_path),
            games=counts[1],
            moves=counts[2],
            spawns=counts[3],
            game_ends=counts[4],
        )

    def close(self) -> None:
        """Flush and close the connection. Idempotent."""
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True
            self._conn.close()
            _log_debug("recorder_closed", db=str(self._db_path))

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _on_game_started(self, event: GameStarted) -> None:
        ts = event.ts if event.ts else time.time()
        if event.experiment_id not in self._known_experiments:
            # Auto-register experiments announced only via the event stream.
            self._exp_rows.append(
                (event.experiment_id, ts, event.kind, event.strategy, _dumps(event.params), "")
            )
            self._known_experiments.add(event.experiment_id)
        self._game_rows.append((event.game_id, event.experiment_id, event.seed, ts))

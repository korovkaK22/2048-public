"""Additive ops tables ``runs`` + ``live_events`` (docs/ops-monitoring.md §2.2).

Deliberately separate from ``SCHEMA_SQL``: the recording contract
(experiments/games/moves/spawns) is frozen; these tables are additive and
owned by the runner's ops layer. ``live_events`` duplicates every alert row
for SQL analytics -- ``alerts.jsonl`` stays the wake-up channel.

Per §2.2 a DB failure must never block the game: all methods raise
:class:`StorageError`, and callers treat that as a ``STORAGE_ERROR`` WARN
(log + keep playing), except at startup where a working DB is required for
the restart budget.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from bot2048.storage.errors import StorageError
from bot2048.storage.schema import connect

__all__ = ["OPS_SCHEMA_SQL", "OpsRepository"]

OPS_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    pid         INTEGER NOT NULL,
    git_rev     TEXT,
    args_json   TEXT NOT NULL DEFAULT '{}',
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS live_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    game_id      TEXT,
    ply          INTEGER,
    level        TEXT NOT NULL,
    code         TEXT NOT NULL,
    message      TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    incident_dir TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_events_run ON live_events(run_id, ts);
"""


class OpsRepository:
    """Writer for ``runs``/``live_events`` in the shared games DB."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        try:
            self._conn = connect(self._db_path)
            self._conn.executescript(OPS_SCHEMA_SQL)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open ops schema at {self._db_path}: {exc}") from exc

    # -- runs ----------------------------------------------------------------

    def start_run(
        self,
        run_id: str,
        pid: int,
        *,
        git_rev: str | None = None,
        args: dict[str, Any] | None = None,
        started_at: float | None = None,
    ) -> None:
        self._execute(
            "INSERT INTO runs (run_id, started_at, pid, git_rev, args_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                started_at if started_at is not None else time.time(),
                pid,
                git_rev,
                json.dumps(args or {}, ensure_ascii=False, sort_keys=True),
            ),
        )

    def end_run(
        self, run_id: str, exit_reason: str, *, ended_at: float | None = None
    ) -> None:
        """``exit_reason``: ``'clean' | 'fatal:<CODE>' | 'killed'``."""
        self._execute(
            "UPDATE runs SET ended_at = ?, exit_reason = ? WHERE run_id = ?",
            (ended_at if ended_at is not None else time.time(), exit_reason, run_id),
        )

    def recent_run_starts(self, since_ts: float) -> list[float]:
        """``started_at`` of runs started at/after ``since_ts`` (for §7 limits)."""
        try:
            rows = self._conn.execute(
                "SELECT started_at FROM runs WHERE started_at >= ? ORDER BY started_at",
                (since_ts,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"recent_run_starts failed: {exc}") from exc
        return [float(r[0]) for r in rows]

    # -- events ----------------------------------------------------------------

    def record_event(
        self,
        run_id: str,
        level: str,
        code: str,
        message: str,
        *,
        game_id: str | None = None,
        ply: int | None = None,
        details: dict[str, Any] | None = None,
        incident_dir: str | None = None,
        ts: float | None = None,
    ) -> None:
        self._execute(
            "INSERT INTO live_events (ts, run_id, game_id, ply, level, code,"
            " message, details_json, incident_dir) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts if ts is not None else time.time(),
                run_id,
                game_id,
                ply,
                level,
                code,
                message,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                incident_dir,
            ),
        )

    # -- restart semantics (§7) -------------------------------------------------

    def mark_interrupted_games(self, *, ended_at: float | None = None) -> int:
        """Close every open game (``ended_at IS NULL``) as ``interrupted``.

        Returns the number of games closed; 0 when the ``games`` table does
        not exist yet (fresh DB, recorder never ran).
        """
        try:
            cur = self._conn.execute(
                "UPDATE games SET ended_at = ?, end_reason = 'interrupted'"
                " WHERE ended_at IS NULL",
                (ended_at if ended_at is not None else time.time(),),
            )
            self._conn.commit()
            return int(cur.rowcount)
        except sqlite3.OperationalError:
            return 0  # no games table yet
        except sqlite3.Error as exc:
            raise StorageError(f"mark_interrupted_games failed: {exc}") from exc

    # -- plumbing ----------------------------------------------------------------

    def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        try:
            self._conn.execute(sql, params)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"ops write failed: {exc}") from exc

    def close(self) -> None:
        """Close the connection. Idempotent."""
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

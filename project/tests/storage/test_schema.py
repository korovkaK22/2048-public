"""Connection pragmas for overnight live runs (feat/db-overnight-hardening).

Every opener of the games DB goes through :func:`bot2048.storage.schema.connect`
(SqliteRecorder, GamesRepository, OpsRepository), so asserting the pragmas on a
fresh connection covers all of them.
"""

from __future__ import annotations

from pathlib import Path

from bot2048.storage.schema import connect

#: PRAGMA synchronous numeric levels (sqlite3.h): OFF=0, NORMAL=1, FULL=2.
SYNCHRONOUS_NORMAL = 1


def _pragma(conn: object, name: str) -> object:
    return conn.execute(f"PRAGMA {name}").fetchone()[0]  # type: ignore[attr-defined]


def test_connect_sets_overnight_pragmas(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        assert _pragma(conn, "journal_mode") == "wal"
        # WAL-safe durability: fsync at checkpoint, not per commit.
        assert _pragma(conn, "synchronous") == SYNCHRONOUS_NORMAL
        # A concurrent connection waits instead of instant "database is locked".
        assert _pragma(conn, "busy_timeout") == 5000
        assert _pragma(conn, "foreign_keys") == 1
        # SQLite default auto-checkpoint must stay enabled so the -wal file
        # cannot grow unboundedly over an all-night run.
        assert int(_pragma(conn, "wal_autocheckpoint")) == 1000  # type: ignore[call-overload]
    finally:
        conn.close()


def test_second_connection_gets_same_pragmas(db_path: Path) -> None:
    """Pragmas are per-connection: the ops/read connections need them too."""
    first = connect(db_path)
    second = connect(db_path)
    try:
        for conn in (first, second):
            assert _pragma(conn, "synchronous") == SYNCHRONOUS_NORMAL
            assert _pragma(conn, "busy_timeout") == 5000
    finally:
        second.close()
        first.close()

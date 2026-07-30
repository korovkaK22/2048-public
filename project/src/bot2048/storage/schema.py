"""SQLite DDL for the telemetry database + compact board hex codec.

Boards are stored as 16-character lowercase hex strings: one hex digit per
cell EXPONENT, row-major (char 0 = top-left cell). "0" = empty, "a" means
2**10 = 1024. The maximum representable tile is 2**15 = 32768 — beyond any
reachable 4x4 game state.

Schema (per docs/architecture.md — do not change without coordination):
    experiments(id, created_at, kind, strategy, params_json, notes)
    games(id, experiment_id, seed, started_at, ended_at, final_score,
          max_exponent, num_moves, end_reason, duration_s)
    moves(game_id, ply, direction, board_before, board_after, score_gained,
          score_total, eval_ms, search_depth, eval_details_json)
    spawns(game_id, ply, cell, exponent, board_after)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bot2048.core.types import BOARD_CELLS, Board
from bot2048.storage.errors import StorageError

MAX_HEX_EXPONENT: int = 15
"""Largest exponent storable in one hex digit (tile 2**15 = 32768)."""

SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS experiments (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    kind        TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS games (
    id            TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    seed          INTEGER,
    started_at    REAL NOT NULL,
    ended_at      REAL,
    final_score   INTEGER,
    max_exponent  INTEGER,
    num_moves     INTEGER,
    end_reason    TEXT,
    duration_s    REAL
);

CREATE TABLE IF NOT EXISTS moves (
    game_id           TEXT NOT NULL REFERENCES games(id),
    ply               INTEGER NOT NULL,
    direction         INTEGER NOT NULL,
    board_before      TEXT NOT NULL,
    board_after       TEXT NOT NULL,
    score_gained      INTEGER NOT NULL,
    score_total      INTEGER NOT NULL,
    eval_ms           REAL NOT NULL,
    search_depth      INTEGER,
    eval_details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS spawns (
    game_id     TEXT NOT NULL REFERENCES games(id),
    ply         INTEGER NOT NULL,
    cell        INTEGER NOT NULL,
    exponent    INTEGER NOT NULL,
    board_after TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_games_experiment_id ON games(experiment_id);
CREATE INDEX IF NOT EXISTS idx_moves_game_id ON moves(game_id);
CREATE INDEX IF NOT EXISTS idx_spawns_game_id ON spawns(game_id);
"""


def board_to_hex(board: Board) -> str:
    """Encode a board (tuple of 16 exponents) as a 16-char hex string."""
    if len(board) != BOARD_CELLS:
        raise StorageError(f"board must have {BOARD_CELLS} cells, got {len(board)}")
    for exponent in board:
        if not 0 <= exponent <= MAX_HEX_EXPONENT:
            raise StorageError(
                f"cell exponent {exponent} outside storable range 0..{MAX_HEX_EXPONENT}"
            )
    return "".join(format(exponent, "x") for exponent in board)


def board_from_hex(text: str) -> Board:
    """Decode a 16-char hex string back into a board tuple."""
    if len(text) != BOARD_CELLS:
        raise StorageError(f"board hex must be {BOARD_CELLS} chars, got {len(text)} ({text!r})")
    try:
        return tuple(int(char, 16) for char in text)
    except ValueError as exc:
        raise StorageError(f"invalid board hex {text!r}") from exc


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the project pragmas.

    Every writer/reader of the games DB (SqliteRecorder, GamesRepository,
    OpsRepository) opens through here, so the pragmas are centralized:

    - ``journal_mode=WAL``: concurrent readers alongside the single writer.
    - ``synchronous=NORMAL``: WAL-safe durability without an fsync on every
      commit. Under WAL, NORMAL fsyncs only at checkpoint time; a power loss
      can drop the last few committed transactions but can NOT corrupt the
      DB. FULL would fsync the WAL per commit -- wasteful for an overnight
      run where the recorder already batches (``executemany`` + per-game
      flush) and telemetry loss of the final in-flight batch is acceptable.
    - ``busy_timeout=5000``: a second connection (ops writer, read-only
      report/percentile queries) waits up to 5 s for the write lock instead
      of failing instantly with "database is locked".
    - ``foreign_keys=ON``: FK enforcement (off by default in SQLite).

    WAL auto-checkpointing stays at the SQLite default (1000 pages, ~4 MB):
    all long-running connections commit regularly and readers are
    short-lived, so the ``-wal`` file cannot grow unboundedly overnight.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indices if missing (idempotent)."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()

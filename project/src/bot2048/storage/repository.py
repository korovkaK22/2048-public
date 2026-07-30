"""Read side of the telemetry store.

Lists experiments, loads game summaries and full replays (moves + spawns
ordered by ply), and computes empirical spawn statistics. Opens its own
SQLite connection; WAL mode makes it safe to read while a single
SqliteRecorder is writing elsewhere.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from bot2048.core.types import Board, Direction
from bot2048.storage.errors import GameNotFoundError, StorageError
from bot2048.storage.schema import board_from_hex, connect


@dataclass(frozen=True, slots=True)
class ExperimentRow:
    """One row of the experiments table (params decoded from JSON)."""

    experiment_id: str
    created_at: float
    kind: str
    strategy: str
    params: dict[str, Any]
    notes: str


@dataclass(frozen=True, slots=True)
class GameSummary:
    """One row of the games table; end-of-game columns are None while running."""

    game_id: str
    experiment_id: str
    seed: int | None
    started_at: float
    ended_at: float | None
    final_score: int | None
    max_exponent: int | None
    num_moves: int | None
    end_reason: str | None
    duration_s: float | None


@dataclass(frozen=True, slots=True)
class MoveRow:
    """One recorded move with boards decoded from hex."""

    ply: int
    direction: Direction
    board_before: Board
    board_after: Board
    score_gained: int
    score_total: int
    eval_ms: float
    search_depth: int | None
    eval_details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SpawnRow:
    """One recorded tile spawn (ply -1/-2 are the two initial tiles)."""

    ply: int
    cell: int
    exponent: int
    board_after: Board


@dataclass(frozen=True, slots=True)
class GameRecord:
    """Full game: summary + all moves and spawns ordered by ply."""

    summary: GameSummary
    moves: tuple[MoveRow, ...]
    spawns: tuple[SpawnRow, ...]


@dataclass(frozen=True, slots=True)
class SpawnStats:
    """Empirical spawn distribution aggregates."""

    total: int
    by_exponent: dict[int, int]
    by_cell: dict[int, int]
    by_cell_exponent: dict[tuple[int, int], int]


_GAME_COLUMNS = (
    "id, experiment_id, seed, started_at, ended_at, final_score, max_exponent, "
    "num_moves, end_reason, duration_s"
)


class GameRepository:
    """Read-only access to a telemetry database. Context-manager friendly."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._closed = False
        if not self._db_path.is_file():
            raise StorageError(f"telemetry database not found: {self._db_path}")
        try:
            self._conn = connect(self._db_path)
        except sqlite3.Error as exc:
            raise StorageError(f"failed to open telemetry db {self._db_path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row

    def list_experiments(self) -> list[ExperimentRow]:
        """All experiments, oldest first."""
        rows = self._fetchall(
            "SELECT id, created_at, kind, strategy, params_json, notes "
            "FROM experiments ORDER BY created_at, id"
        )
        return [
            ExperimentRow(
                experiment_id=row["id"],
                created_at=row["created_at"],
                kind=row["kind"],
                strategy=row["strategy"],
                params=json.loads(row["params_json"]),
                notes=row["notes"],
            )
            for row in rows
        ]

    def list_games(self, experiment_id: str | None = None) -> list[GameSummary]:
        """Game summaries, optionally restricted to one experiment."""
        if experiment_id is None:
            rows = self._fetchall(f"SELECT {_GAME_COLUMNS} FROM games ORDER BY started_at, id")
        else:
            rows = self._fetchall(
                f"SELECT {_GAME_COLUMNS} FROM games WHERE experiment_id = ? "
                "ORDER BY started_at, id",
                (experiment_id,),
            )
        return [self._summary(row) for row in rows]

    def load_game(self, game_id: str) -> GameRecord:
        """Full game record; raises GameNotFoundError for unknown ids."""
        rows = self._fetchall(f"SELECT {_GAME_COLUMNS} FROM games WHERE id = ?", (game_id,))
        if not rows:
            raise GameNotFoundError(f"game {game_id!r} not found in {self._db_path}")
        summary = self._summary(rows[0])
        move_rows = self._fetchall(
            "SELECT ply, direction, board_before, board_after, score_gained, score_total, "
            "eval_ms, search_depth, eval_details_json FROM moves WHERE game_id = ? ORDER BY ply",
            (game_id,),
        )
        moves = tuple(
            MoveRow(
                ply=row["ply"],
                direction=Direction(row["direction"]),
                board_before=board_from_hex(row["board_before"]),
                board_after=board_from_hex(row["board_after"]),
                score_gained=row["score_gained"],
                score_total=row["score_total"],
                eval_ms=row["eval_ms"],
                search_depth=row["search_depth"],
                eval_details=json.loads(row["eval_details_json"]),
            )
            for row in move_rows
        )
        spawn_rows = self._fetchall(
            "SELECT ply, cell, exponent, board_after FROM spawns WHERE game_id = ? "
            "ORDER BY ply, cell",
            (game_id,),
        )
        spawns = tuple(
            SpawnRow(
                ply=row["ply"],
                cell=row["cell"],
                exponent=row["exponent"],
                board_after=board_from_hex(row["board_after"]),
            )
            for row in spawn_rows
        )
        return GameRecord(summary=summary, moves=moves, spawns=spawns)

    def spawn_stats(
        self,
        experiment_id: str | None = None,
        *,
        include_initial: bool = True,
    ) -> SpawnStats:
        """Spawn counts by exponent, by cell and by (cell, exponent)."""
        join = ""
        conditions: list[str] = []
        params: list[object] = []
        if experiment_id is not None:
            join = " JOIN games AS g ON g.id = s.game_id"
            conditions.append("g.experiment_id = ?")
            params.append(experiment_id)
        if not include_initial:
            conditions.append("s.ply >= 0")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._fetchall(
            f"SELECT s.cell AS cell, s.exponent AS exponent, COUNT(*) AS n "
            f"FROM spawns AS s{join}{where} GROUP BY s.cell, s.exponent",
            tuple(params),
        )
        total = 0
        by_exponent: dict[int, int] = {}
        by_cell: dict[int, int] = {}
        by_cell_exponent: dict[tuple[int, int], int] = {}
        for row in rows:
            cell, exponent, count = row["cell"], row["exponent"], row["n"]
            total += count
            by_exponent[exponent] = by_exponent.get(exponent, 0) + count
            by_cell[cell] = by_cell.get(cell, 0) + count
            by_cell_exponent[(cell, exponent)] = count
        return SpawnStats(
            total=total,
            by_exponent=by_exponent,
            by_cell=by_cell,
            by_cell_exponent=by_cell_exponent,
        )

    def avg_eval_ms(self, experiment_id: str | None = None) -> float | None:
        """Mean per-move evaluation time; None if there are no moves."""
        if experiment_id is None:
            rows = self._fetchall("SELECT AVG(eval_ms) AS avg_ms FROM moves")
        else:
            rows = self._fetchall(
                "SELECT AVG(m.eval_ms) AS avg_ms FROM moves AS m "
                "JOIN games AS g ON g.id = m.game_id WHERE g.experiment_id = ?",
                (experiment_id,),
            )
        value = rows[0]["avg_ms"] if rows else None
        return float(value) if value is not None else None

    def close(self) -> None:
        """Close the connection. Idempotent."""
        if self._closed:
            return
        self._closed = True
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

    def _fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        try:
            return list(self._conn.execute(sql, params).fetchall())
        except sqlite3.Error as exc:
            raise StorageError(f"query failed on {self._db_path}: {exc}") from exc

    @staticmethod
    def _summary(row: sqlite3.Row) -> GameSummary:
        return GameSummary(
            game_id=row["id"],
            experiment_id=row["experiment_id"],
            seed=row["seed"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            final_score=row["final_score"],
            max_exponent=row["max_exponent"],
            num_moves=row["num_moves"],
            end_reason=row["end_reason"],
            duration_s=row["duration_s"],
        )

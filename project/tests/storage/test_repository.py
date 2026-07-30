"""Round-trip: synthetic event stream -> SqliteRecorder -> GameRepository."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import EXPERIMENT_ID, GAME_ID, synthetic_events

from bot2048.core.types import Direction
from bot2048.recording import MovePlayed, TileSpawned
from bot2048.storage import GameNotFoundError, GameRepository, StorageError


def test_experiment_round_trip(synthetic_db: Path) -> None:
    with GameRepository(synthetic_db) as repo:
        experiments = repo.list_experiments()
        assert [e.experiment_id for e in experiments] == [EXPERIMENT_ID]
        exp = experiments[0]
        assert exp.kind == "sim"
        assert exp.strategy == "greedy"
        assert exp.params == {"depth": 1, "seed": 42}
        assert exp.notes == "unit-test experiment"


def test_game_summary_round_trip(synthetic_db: Path) -> None:
    with GameRepository(synthetic_db) as repo:
        games = repo.list_games(EXPERIMENT_ID)
        assert len(games) == 1
        game = games[0]
        assert game.game_id == GAME_ID
        assert game.experiment_id == EXPERIMENT_ID
        assert game.seed == 42
        assert game.started_at == pytest.approx(100.0)
        assert game.ended_at == pytest.approx(101.5)
        assert game.final_score == 4
        assert game.max_exponent == 2
        assert game.num_moves == 3
        assert game.end_reason == "aborted"
        assert game.duration_s == pytest.approx(1.5)


def test_full_game_round_trip(synthetic_db: Path) -> None:
    events = synthetic_events()
    expected_moves = [e for e in events if isinstance(e, MovePlayed)]
    expected_spawns = sorted(
        (e for e in events if isinstance(e, TileSpawned)), key=lambda e: e.ply
    )
    with GameRepository(synthetic_db) as repo:
        record = repo.load_game(GAME_ID)

    assert [m.ply for m in record.moves] == [0, 1, 2]
    for row, event in zip(record.moves, expected_moves, strict=True):
        assert row.ply == event.ply
        assert isinstance(row.direction, Direction)
        assert row.direction == event.direction
        assert row.board_before == event.board_before
        assert row.board_after == event.board_after
        assert row.score_gained == event.score_gained
        assert row.score_total == event.score_total
        assert row.eval_ms == pytest.approx(event.eval_ms)
        assert row.search_depth == event.search_depth
        assert row.eval_details == event.eval_details

    assert [s.ply for s in record.spawns] == [-2, -1, 0, 1, 2]
    for row, event in zip(record.spawns, expected_spawns, strict=True):
        assert (row.ply, row.cell, row.exponent) == (
            event.ply,
            event.spawn.cell,
            event.spawn.exponent,
        )
        assert row.board_after == event.board_after


def test_load_game_unknown_id(synthetic_db: Path) -> None:
    with GameRepository(synthetic_db) as repo, pytest.raises(GameNotFoundError):
        repo.load_game("no-such-game")


def test_missing_db_raises(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        GameRepository(tmp_path / "absent.db")


def test_spawn_stats(synthetic_db: Path) -> None:
    with GameRepository(synthetic_db) as repo:
        stats = repo.spawn_stats()
        assert stats.total == 5
        assert stats.by_exponent == {1: 4, 2: 1}
        assert stats.by_cell == {3: 1, 12: 1, 15: 1, 8: 1, 2: 1}
        assert stats.by_cell_exponent[(15, 2)] == 1

        moves_only = repo.spawn_stats(include_initial=False)
        assert moves_only.total == 3
        assert moves_only.by_exponent == {1: 2, 2: 1}

        filtered = repo.spawn_stats(EXPERIMENT_ID)
        assert filtered == stats
        assert repo.spawn_stats("no-such-experiment").total == 0


def test_avg_eval_ms(synthetic_db: Path) -> None:
    with GameRepository(synthetic_db) as repo:
        avg = repo.avg_eval_ms(EXPERIMENT_ID)
        assert avg == pytest.approx((1.25 + 2.5 + 0.75) / 3)
        assert repo.avg_eval_ms("no-such-experiment") is None

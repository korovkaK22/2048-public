"""SqliteRecorder: batching/flush semantics, lifecycle, auto-registration."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import EXPERIMENT, GAME_ID, synthetic_events

from bot2048.storage import GameRepository, RecorderError, SqliteRecorder


def test_flush_persists_for_second_connection(db_path: Path) -> None:
    events = synthetic_events()
    recorder = SqliteRecorder(db_path, EXPERIMENT, batch_size=10_000)
    try:
        for event in events[:5]:  # start, 2 initial spawns, move 0, spawn 0
            recorder.record(event)
        recorder.flush()
        with GameRepository(db_path) as repo:  # independent connection (WAL)
            games = repo.list_games()
            assert [g.game_id for g in games] == [GAME_ID]
            record = repo.load_game(GAME_ID)
            assert len(record.moves) == 1
            assert len(record.spawns) == 3
    finally:
        recorder.close()


def test_game_ended_triggers_flush(db_path: Path) -> None:
    recorder = SqliteRecorder(db_path, EXPERIMENT, batch_size=10_000)
    try:
        for event in synthetic_events():
            recorder.record(event)
        # No explicit flush: GameEnded must have flushed everything.
        with GameRepository(db_path) as repo:
            summary = repo.list_games()[0]
            assert summary.final_score == 4
            assert summary.end_reason == "aborted"
            assert len(repo.load_game(GAME_ID).moves) == 3
    finally:
        recorder.close()


def test_small_batch_size_flushes_incrementally(db_path: Path) -> None:
    recorder = SqliteRecorder(db_path, EXPERIMENT, batch_size=2)
    try:
        for event in synthetic_events()[:5]:
            recorder.record(event)
        with GameRepository(db_path) as repo:  # >= 4 events already flushed
            assert repo.list_games()
    finally:
        recorder.close()


def test_close_is_idempotent_and_record_after_close_raises(db_path: Path) -> None:
    recorder = SqliteRecorder(db_path, EXPERIMENT)
    recorder.record(synthetic_events()[0])
    recorder.close()
    recorder.close()  # idempotent
    recorder.flush()  # no-op after close
    with pytest.raises(RecorderError):
        recorder.record(synthetic_events()[1])


def test_context_manager_flushes_on_exit(synthetic_db: Path) -> None:
    with GameRepository(synthetic_db) as repo:
        assert repo.list_games()[0].num_moves == 3


def test_auto_registers_experiment_from_events(db_path: Path) -> None:
    with SqliteRecorder(db_path, EXPERIMENT) as recorder:
        for event in synthetic_events():
            recorder.record(event)
        for event in synthetic_events(game_id="game-0002", experiment_id="exp-2"):
            recorder.record(event)
    with GameRepository(db_path) as repo:
        ids = {e.experiment_id for e in repo.list_experiments()}
        assert ids == {"exp-1", "exp-2"}
        assert [g.game_id for g in repo.list_games("exp-2")] == ["game-0002"]


def test_reopening_same_experiment_does_not_duplicate(db_path: Path) -> None:
    for _ in range(2):
        with SqliteRecorder(db_path, EXPERIMENT) as recorder:
            del recorder
    with GameRepository(db_path) as repo:
        assert len(repo.list_experiments()) == 1

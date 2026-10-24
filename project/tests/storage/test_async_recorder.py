"""AsyncRecorder: ordering, flush marker, never-drop, failure surfacing, close.

The inner recorder is a probing ``ListRecorder`` subclass (thread id of every
``record``, flush/close counters, optional per-event delay / gate / injected
failures). Real SQLite only where the writer-thread ownership of the connection
is the point (factory test + the FakePhone live loop).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import pytest
import structlog

from bot2048.core.types import Direction
from bot2048.recording import GameEvent, ListRecorder, MovePlayed, Recorder
from bot2048.storage import AsyncRecorder, GameRepository, RecorderError, SqliteRecorder
from bot2048.storage.errors import StorageError
from tests.storage.conftest import EXPERIMENT, GAME_ID, synthetic_events


class ProbeRecorder(ListRecorder):
    """ListRecorder that records WHO called it and can stall or fail on demand."""

    def __init__(
        self,
        *,
        delay_s: float = 0.0,
        gate: threading.Event | None = None,
        fail_record: Callable[[GameEvent], bool] | None = None,
        fail_flush_times: int = 0,
    ) -> None:
        super().__init__()
        self.delay_s = delay_s
        self.gate = gate
        self.fail_record = fail_record
        self.fail_flush_times = fail_flush_times
        self.record_threads: set[int] = set()
        self.flushes = 0
        self.closes = 0
        self.flush_failures = 0
        self._lock = threading.Lock()

    def record(self, event: GameEvent) -> None:
        self.record_threads.add(threading.get_ident())
        if self.gate is not None:
            self.gate.wait()
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail_record is not None and self.fail_record(event):
            raise RecorderError("injected record failure")
        with self._lock:
            super().record(event)

    def flush(self) -> None:
        with self._lock:
            self.flushes += 1
            if self.fail_flush_times > 0:
                self.fail_flush_times -= 1
                self.flush_failures += 1
                raise RecorderError("injected flush failure")

    def close(self) -> None:
        self.closes += 1


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# -- ordering / threading ------------------------------------------------------------


def test_events_reach_inner_in_order_on_the_writer_thread() -> None:
    probe = ProbeRecorder()
    events = synthetic_events()
    with AsyncRecorder(probe) as rec:
        for event in events:
            rec.record(event)
        assert rec.flush_sync(timeout=5.0)
        assert probe.events == events  # same objects, same order
        assert probe.record_threads == {rec._writer.ident}
        assert threading.get_ident() not in probe.record_threads
    assert rec.stats.enqueued == len(events)
    assert rec.stats.written == len(events)
    assert rec.stats.backpressure == 0
    assert rec.stats.failures == 0


def test_isinstance_recorder_protocol() -> None:
    with AsyncRecorder(ProbeRecorder()) as rec:
        assert isinstance(rec, Recorder)


# -- flush marker -------------------------------------------------------------------


def test_flush_returns_immediately_while_writer_is_stalled() -> None:
    gate = threading.Event()
    probe = ProbeRecorder(gate=gate)
    rec = AsyncRecorder(probe, flush_every_n=1000, flush_every_s=100.0)
    try:
        rec.record(synthetic_events()[0])  # the writer now blocks inside inner.record
        assert _wait_until(lambda: bool(probe.record_threads))
        t0 = time.monotonic()
        rec.flush()  # marker only: must not wait for the stalled writer
        assert time.monotonic() - t0 < 0.5
        assert probe.flushes == 0
        gate.set()
        assert _wait_until(lambda: probe.flushes >= 1)
    finally:
        gate.set()
        rec.close()


def test_flush_sync_waits_for_the_marker_and_times_out_when_stalled() -> None:
    gate = threading.Event()
    probe = ProbeRecorder(gate=gate)
    rec = AsyncRecorder(probe, flush_every_n=1000, flush_every_s=100.0)
    try:
        rec.record(synthetic_events()[0])
        assert rec.flush_sync(timeout=0.2) is False  # stalled writer -> timeout, no hang
        gate.set()
        assert rec.flush_sync(timeout=5.0) is True
        assert probe.events == synthetic_events()[:1]
    finally:
        gate.set()
        rec.close()


def test_count_based_batching() -> None:
    probe = ProbeRecorder()
    with AsyncRecorder(probe, flush_every_n=4, flush_every_s=100.0) as rec:
        for event in synthetic_events()[:8]:
            rec.record(event)
        assert _wait_until(lambda: probe.flushes >= 2)
        assert len(probe.events) == 8


def test_age_based_batching() -> None:
    probe = ProbeRecorder()
    with AsyncRecorder(probe, flush_every_n=1000, flush_every_s=0.02) as rec:
        for event in synthetic_events()[:3]:
            rec.record(event)
        # no marker, no count trigger: the 20 ms age timer must flush on its own
        assert _wait_until(lambda: probe.flushes >= 1)
        assert len(probe.events) == 3


# -- never drop -------------------------------------------------------------------------


def test_full_queue_blocks_with_backpressure_and_loses_nothing() -> None:
    probe = ProbeRecorder(delay_s=0.002)  # deliberately slow writer: ~2 ms per event
    base = synthetic_events()[3]
    assert isinstance(base, MovePlayed)
    events = [
        MovePlayed(
            game_id=base.game_id,
            ply=ply,
            direction=Direction.LEFT,
            board_before=base.board_before,
            board_after=base.board_after,
            score_gained=0,
            score_total=0,
            eval_ms=1.0,
        )
        for ply in range(200)
    ]
    with structlog.testing.capture_logs() as logs, AsyncRecorder(probe, maxsize=4) as rec:
        t0 = time.monotonic()
        for event in events:
            rec.record(event)  # must block on Full, never raise, never drop
        enqueue_s = time.monotonic() - t0
        assert rec.flush_sync(timeout=10.0)
        stats = rec.stats
    assert probe.events == events  # every event, in order
    assert stats.backpressure > 0  # the loop thread did stall...
    assert enqueue_s > 0.1  # ...for real (200 x 2 ms of writer time)
    assert stats.enqueued == stats.written == 200
    warns = [entry for entry in logs if entry["event"] == "recorder.backpressure"]
    assert warns, "backpressure must be WARN-logged, not silent"
    assert warns[0]["log_level"] == "warning"
    assert warns[0]["maxsize"] == 4


# -- failure surfacing ---------------------------------------------------------------


def test_record_failure_surfaces_on_next_record_then_writer_keeps_going() -> None:
    events = synthetic_events()
    poison = events[3]  # move 0
    probe = ProbeRecorder(fail_record=lambda e: e is poison)
    rec = AsyncRecorder(probe, flush_every_n=1000, flush_every_s=100.0)
    try:
        for event in events[:4]:
            rec.record(event)
        assert _wait_until(lambda: rec.stats.failures >= 1)
        with pytest.raises(StorageError) as exc_info:
            rec.record(events[4])  # reports the background failure...
        assert "injected record failure" in str(exc_info.value)
        assert isinstance(exc_info.value, RecorderError)
        rec.record(events[5])  # ...exactly once: the next call is clean
        assert rec.flush_sync(timeout=5.0)
        # the reporting call's event was enqueued BEFORE the raise -- not lost
        assert probe.events == [e for e in events[:6] if e is not poison]
        assert rec.stats.failures == 1
    finally:
        rec.close()


def test_flush_failure_surfaces_and_is_retried_with_backoff() -> None:
    probe = ProbeRecorder(fail_flush_times=2)
    rec = AsyncRecorder(probe, flush_every_n=1000, flush_every_s=0.01)
    try:
        rec.record(synthetic_events()[0])
        # the age timer flushes -> fails twice (50 ms + 100 ms backoff) -> succeeds
        assert _wait_until(lambda: probe.flushes >= 3 and probe.flush_failures == 2)
        with pytest.raises(StorageError):
            rec.record(synthetic_events()[1])
        rec.record(synthetic_events()[2])  # reported once, then clean
        assert rec.flush_sync(timeout=5.0)
        assert len(probe.events) == 3
    finally:
        rec.close()


def test_open_failure_raises_in_constructor() -> None:
    def boom() -> Recorder:
        raise RecorderError("cannot open (test)")

    before = threading.active_count()
    with pytest.raises(RecorderError, match="cannot open"):
        AsyncRecorder(boom)
    assert _wait_until(lambda: threading.active_count() <= before)


def test_sqlite_recorder_instance_is_refused(db_path: Path) -> None:
    """A ready SqliteRecorder would carry its connection across threads: refuse it."""
    inner = SqliteRecorder(db_path, EXPERIMENT)
    try:
        with pytest.raises(TypeError, match="factory"):
            AsyncRecorder(inner)
    finally:
        inner.close()


# -- close ---------------------------------------------------------------------------------


def test_close_flushes_closes_inner_on_writer_thread_joins_and_is_idempotent() -> None:
    probe = ProbeRecorder()
    rec = AsyncRecorder(probe, flush_every_n=1000, flush_every_s=100.0)
    for event in synthetic_events():
        rec.record(event)
    rec.close()
    assert not rec._writer.is_alive()
    assert probe.events == synthetic_events()
    assert probe.flushes >= 1
    assert probe.closes == 1
    rec.close()  # idempotent
    assert probe.closes == 1
    rec.flush()  # no-op after close
    assert rec.flush_sync() is True
    with pytest.raises(RecorderError):
        rec.record(synthetic_events()[0])


def test_close_times_out_on_a_stuck_writer_without_hanging() -> None:
    gate = threading.Event()
    probe = ProbeRecorder(gate=gate)
    rec = AsyncRecorder(probe, maxsize=2)
    try:
        rec.record(synthetic_events()[0])  # writer blocks in inner.record
        rec.record(synthetic_events()[1])
        rec.record(synthetic_events()[2])  # queue now full (maxsize 2)
        t0 = time.monotonic()
        with structlog.testing.capture_logs() as logs:
            rec.close(timeout=0.3)
        assert time.monotonic() - t0 < 2.0
        assert any(e["event"] == "recorder.close_timeout" for e in logs)
        assert probe.closes == 0
    finally:
        gate.set()  # let the daemon writer drain and exit
    assert _wait_until(lambda: probe.closes == 1)


# -- real SQLite through the factory ----------------------------------------------------


def test_sqlite_factory_opens_and_closes_on_the_writer_thread(db_path: Path) -> None:
    rec = AsyncRecorder(lambda: SqliteRecorder(db_path, EXPERIMENT))
    try:
        for event in synthetic_events():
            rec.record(event)
    finally:
        rec.close()
    with GameRepository(db_path) as repo:
        summary = repo.list_games()[0]
        assert summary.game_id == GAME_ID
        assert summary.num_moves == 3
        assert len(repo.load_game(GAME_ID).moves) == 3


def test_fake_phone_live_loop_persists_every_move(tmp_path: Path) -> None:
    """End-to-end: LiveRunner -> AsyncRecorder -> SQLite; num_moves == COUNT(moves)."""
    from bot2048.runner.live import LiveConfig
    from bot2048.storage.sqlite_recorder import ExperimentMeta
    from tests.runner.conftest import FakePhone, ScriptedStrategy, make_harness

    open_board = (1, 1) + (0,) * 14
    db = tmp_path / "live.db"
    meta = ExperimentMeta(experiment_id="t", kind="live", strategy="scripted")
    rec = AsyncRecorder(lambda: SqliteRecorder(db, meta))
    phone = FakePhone(board=open_board, spawn_script=deque())
    h = make_harness(
        tmp_path,
        phone,
        strategy=ScriptedStrategy([Direction.RIGHT, Direction.LEFT] * 4),
        config=LiveConfig(experiment_id="t", max_games=1, max_moves_per_game=6),
        recorder=rec,
    )
    try:
        h.runner.run()
    finally:
        rec.close()
    assert rec.stats.backpressure == 0
    assert rec.stats.failures == 0

    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        (game_id, num_moves, end_reason, ended_at) = conn.execute(
            "SELECT id, num_moves, end_reason, ended_at FROM games"
        ).fetchone()
        (move_rows,) = conn.execute(
            "SELECT COUNT(*) FROM moves WHERE game_id = ?", (game_id,)
        ).fetchone()
        (spawn_rows,) = conn.execute(
            "SELECT COUNT(*) FROM spawns WHERE game_id = ? AND ply >= 0", (game_id,)
        ).fetchone()
    finally:
        conn.close()
    assert num_moves == 6
    assert move_rows == num_moves
    assert spawn_rows == num_moves
    assert end_reason == "aborted"  # max_moves_per_game cap
    assert ended_at is not None  # GameEnded went through the same queue: row finalized

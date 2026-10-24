"""setup_logging: JSONL file output, bound context, level gating, async flush."""

from __future__ import annotations

import io
import json
import logging
import sys
import threading
import time
from pathlib import Path

import pytest
import structlog

from bot2048.logging_setup import (
    LOG_QUEUE_MAXSIZE,
    bind_game_context,
    clear_game_context,
    setup_logging,
    stop_logging,
)


def _teardown_logging() -> None:
    stop_logging()  # drain + join the async pump before tearing handlers down
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    structlog.reset_defaults()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Flush the async queue, then parse every JSONL line."""
    stop_logging()  # the file write happens on the listener thread -- drain it
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_jsonl_file_and_context(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    try:
        jsonl_path = setup_logging(json_logs=True, log_dir=log_dir)
        assert jsonl_path is not None
        assert jsonl_path.parent == log_dir
        assert jsonl_path.name.startswith("bot2048-")
        assert jsonl_path.suffix == ".jsonl"

        bind_game_context("game-xyz", ply=7)
        structlog.get_logger("bot2048.test").info("move_recorded", score=42)
        clear_game_context()
        structlog.get_logger("bot2048.test").info("after_clear")

        lines = _read_jsonl(jsonl_path)
        assert len(lines) == 2
        first = lines[0]
        assert first["event"] == "move_recorded"
        assert first["game_id"] == "game-xyz"
        assert first["ply"] == 7
        assert first["score"] == 42
        assert first["level"] == "info"
        second = lines[1]
        assert "game_id" not in second
    finally:
        _teardown_logging()


def test_console_only_mode(tmp_path: Path) -> None:
    try:
        assert setup_logging(json_logs=False, log_dir=tmp_path / "logs") is None
        assert not (tmp_path / "logs").exists()
    finally:
        _teardown_logging()


def test_default_info_level_gates_debug(tmp_path: Path) -> None:
    """At the default INFO run level a log.debug line is dropped, INFO survives."""
    log_dir = tmp_path / "logs"
    try:
        jsonl_path = setup_logging(json_logs=True, log_dir=log_dir)
        assert jsonl_path is not None
        log = structlog.get_logger("bot2048.test")
        # The high-frequency per-move debug lines that are gated off the hot path.
        log.debug("adb.swipe", direction="LEFT")
        log.debug("live.apk_gate_hit", frames=1)
        log.debug("vision.cell.color_fallback", cell=3)
        # The must-keep per-move cadence line stays at INFO.
        log.info("live.move_timing", total_ms=305.0, ply=42, swipe_attempts=1)

        events = [line["event"] for line in _read_jsonl(jsonl_path)]
        assert events == ["live.move_timing"]
    finally:
        _teardown_logging()


def test_verbose_debug_level_emits_hot_path_lines(tmp_path: Path) -> None:
    """--verbose (DEBUG) brings the gated per-move debug lines back."""
    log_dir = tmp_path / "logs"
    try:
        jsonl_path = setup_logging(json_logs=True, log_dir=log_dir, level=logging.DEBUG)
        assert jsonl_path is not None
        log = structlog.get_logger("bot2048.test")
        log.debug("adb.swipe", direction="LEFT")
        log.info("live.move_timing", total_ms=305.0, ply=42)

        events = [line["event"] for line in _read_jsonl(jsonl_path)]
        assert events == ["adb.swipe", "live.move_timing"]
    finally:
        _teardown_logging()


def test_console_no_ansi_when_stdout_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirected (non-tty) stdout gets plain text -- no ANSI color escapes.

    Live runs redirect stdout to the golden-log file; ANSI codes there split
    field values (``\\x1b[36mtotal_ms\\x1b[0m=...``) and break grep/awk parsing.
    The console handler binds ``sys.stdout`` at setup time, so the non-tty
    buffer must replace it BEFORE :func:`setup_logging`.
    """
    buffer = io.StringIO()  # StringIO.isatty() is False -> colors must be off
    monkeypatch.setattr(sys, "stdout", buffer)
    try:
        assert setup_logging(json_logs=False) is None
        structlog.get_logger("bot2048.test").info(
            "live.move_timing", total_ms=344.0, ply=42
        )
        stop_logging()  # console write runs on the listener thread -- drain it

        captured = buffer.getvalue()
        assert "live.move_timing" in captured
        assert "\x1b[" not in captured
    finally:
        _teardown_logging()


class _FrozenStdout(io.StringIO):
    """stdout stand-in whose ``write`` blocks until released.

    Models the Windows console QuickEdit "mark" freeze (or a stalled
    ``tail | grep``): the console StreamHandler on the listener thread hangs in
    ``write``, so nothing is dequeued until :attr:`release` is set.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()  # the listener is now stuck in write()
        self.release = threading.Event()

    def write(self, s: str) -> int:
        self.entered.set()
        self.release.wait()
        return super().write(s)

    def isatty(self) -> bool:
        return False


def test_default_queue_is_bounded(tmp_path: Path) -> None:
    try:
        setup_logging(json_logs=False, log_dir=tmp_path / "logs")
        (handler,) = logging.getLogger().handlers
        assert isinstance(handler, logging.handlers.QueueHandler)
        assert handler.queue.maxsize == LOG_QUEUE_MAXSIZE  # type: ignore[attr-defined]
    finally:
        _teardown_logging()


def test_full_queue_drops_info_blocks_warn_briefly_and_reports_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocked console: INFO past the bound is dropped + counted, WARN waits <= 10 ms,
    and the listener emits ONE ``logging.dropped n=...`` once there is room again."""
    frozen = _FrozenStdout()
    monkeypatch.setattr(sys, "stdout", frozen)
    log_dir = tmp_path / "logs"
    try:
        jsonl_path = setup_logging(json_logs=True, log_dir=log_dir, queue_maxsize=4)
        assert jsonl_path is not None
        log = structlog.get_logger("bot2048.test")
        log.info("first")  # the listener dequeues it and freezes in stdout.write
        assert frozen.entered.wait(5.0)
        for i in range(4):
            log.info("fill", i=i)  # exactly fills the bounded queue
        for i in range(10):
            log.info("overflow", i=i)  # Full -> dropped immediately, counted
        t0 = time.monotonic()
        log.warning("important")  # Full -> waits WARN_BLOCK_S for room, then dropped
        waited = time.monotonic() - t0
        assert 0.005 <= waited < 1.0

        frozen.release.set()
        assert stop_logging(timeout=10.0) == 0  # clean drain once the console thaws

        lines = [json.loads(line) for line in jsonl_path.read_text("utf-8").splitlines()]
        events = [line["event"] for line in lines]
        assert events == ["first", "fill", "fill", "fill", "fill", "logging.dropped"]
        dropped = lines[-1]
        assert dropped["level"] == "warning"
        assert dropped["n"] == 11  # 10 INFO + the 1 WARN that could not get in
        assert dropped["maxsize"] == 4
    finally:
        frozen.release.set()
        _teardown_logging()


def test_stop_logging_gives_up_on_a_stuck_consumer_and_reports_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A console that never thaws must not hang shutdown: bounded, remainder returned."""
    frozen = _FrozenStdout()
    monkeypatch.setattr(sys, "stdout", frozen)
    try:
        setup_logging(json_logs=False, log_dir=tmp_path / "logs", queue_maxsize=3)
        log = structlog.get_logger("bot2048.test")
        log.info("first")
        assert frozen.entered.wait(5.0)
        for i in range(3):
            log.info("fill", i=i)
        t0 = time.monotonic()
        left_behind = stop_logging(timeout=0.3)
        elapsed = time.monotonic() - t0
        assert left_behind == 3  # the 3 queued records (the sentinel never fitted)
        assert elapsed < 3.0
        assert stop_logging() == 0  # idempotent: nothing to stop any more
    finally:
        frozen.release.set()  # let the abandoned daemon thread drain and go idle
        _teardown_logging()


def test_must_keep_events_emit_at_info(tmp_path: Path) -> None:
    """The health-monitor's must-keep events survive the default INFO level.

    Guards the monitoring contract (docs/ops-monitoring.md): move cadence, game
    lifecycle, and the adb-forward-drop detector are all >= INFO, so gating the
    hot-path debug noise never hides them.
    """
    log_dir = tmp_path / "logs"
    try:
        jsonl_path = setup_logging(json_logs=True, log_dir=log_dir)
        assert jsonl_path is not None
        log = structlog.get_logger("bot2048.test")
        log.info("live.move_timing", total_ms=305.0, ply=1, swipe_attempts=1)
        log.info("live.game_started", game_id="g", initial_tiles=2)
        log.info("live.game_finished", game_id="g", end_reason="win_2048")
        log.warning("apk.connect_failed", port=1234)

        events = [line["event"] for line in _read_jsonl(jsonl_path)]
        assert events == [
            "live.move_timing",
            "live.game_started",
            "live.game_finished",
            "apk.connect_failed",
        ]
    finally:
        _teardown_logging()

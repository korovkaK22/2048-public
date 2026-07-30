"""setup_logging: JSONL file output, bound context, level gating, async flush."""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import pytest
import structlog

from bot2048.logging_setup import (
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

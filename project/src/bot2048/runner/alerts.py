"""Alert stream ``logs/alerts.jsonl`` -- schema v1 (docs/ops-monitoring.md §2.3, §3).

Only WARN/ERROR/FATAL go here; INFO/DEBUG stays in the daily JSONL. The file
is strictly append-only for the bot -- rotation is the operator's job and
only while the runner is stopped. One compact JSON object per line, UTF-8
WITHOUT BOM, LF endings, ``flush()`` + ``os.fsync()`` after every line, so
``python -m bot2048.tgnotify watch`` sees either a whole line or nothing.

Schema evolution: adding codes/fields is fine; renaming or re-typing a field
requires bumping :data:`ALERT_SCHEMA_VERSION`. The monitor filters only on
``level``, so new codes never break it.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

__all__ = [
    "ALERT_SCHEMA_VERSION",
    "DEFAULT_ALERTS_PATH",
    "Alert",
    "AlertCode",
    "AlertLevel",
    "AlertWriter",
    "utc_now_iso",
]

ALERT_SCHEMA_VERSION: int = 1

DEFAULT_ALERTS_PATH: Path = Path("logs") / "alerts.jsonl"


class AlertLevel(StrEnum):
    """Escalation levels; the monitor wakes the operator on ERROR/FATAL."""

    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"


class AlertCode(StrEnum):
    """Failure taxonomy (§3). Per-code policy lives in runner.escalation."""

    DEVICE_LOST = "DEVICE_LOST"  # adb does not see the device / adb command fails
    AUTH_REQUIRED = "AUTH_REQUIRED"  # bank login wall (screencap 0 bytes x3 + ui-dump anchor)
    VISION_MISMATCH = "VISION_MISMATCH"  # parsed board contradicts the expected one
    MOVE_REJECTED = "MOVE_REJECTED"  # legal swipe did not change the board
    GAME_OVER_UNEXPECTED = "GAME_OVER_UNEXPECTED"  # is_game_over vs strategy/popup mismatch
    SCREEN_UNEXPECTED = "SCREEN_UNEXPECTED"  # persistent NOT_IN_GAME / SCREEN_OFF / UNKNOWN
    CRASH = "CRASH"  # uncaught runner exception (top-level except)
    WATCHDOG_TIMEOUT = "WATCHDOG_TIMEOUT"  # synthesized by the monitor: heartbeat stale
    STORAGE_ERROR = "STORAGE_ERROR"  # SQLite write failure (game keeps going)
    RESTART_LOOP = "RESTART_LOOP"  # hourly restart limit exceeded
    LOOP_ERROR = "LOOP_ERROR"  # live loop recovered in place from an unexpected exception


def utc_now_iso() -> str:
    """ISO-8601 UTC with microseconds and ``Z`` suffix (the ``ts`` field)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True, slots=True)
class Alert:
    """One alert row (schema v1). ``None`` fields are emitted as JSON null."""

    level: AlertLevel
    code: AlertCode
    message: str
    run_id: str | None = None
    game_id: str | None = None
    move_no: int | None = None
    screen_state: str | None = None  # ScreenState.value, or None if no frame
    board_hex: str | None = None  # 16 hex chars, as in storage
    attempt: int | None = None  # attempt number within this failure
    incident_dir: str | None = None  # relative path, None for WARN
    extra: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ALERT_SCHEMA_VERSION,
            "ts": self.ts,
            "level": self.level.value,
            "code": self.code.value,
            "message": self.message,
            "run_id": self.run_id,
            "game_id": self.game_id,
            "move_no": self.move_no,
            "screen_state": self.screen_state,
            "board_hex": self.board_hex,
            "attempt": self.attempt,
            "incident_dir": self.incident_dir,
            "extra": self.extra,
        }

    def to_json_line(self) -> str:
        """Compact single-line JSON. ``json.dumps`` escapes control chars,
        so embedded newlines in ``message`` can never split the line."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


class AlertWriter:
    """Append-only, thread-safe ``alerts.jsonl`` writer (fsync per line).

    Args:
        path: the alerts stream.
        notifier: optional fire-and-forget hook called AFTER a successful
            write (docs/telegram-notify.md §3.2) -- the runner wires
            ``bot2048.tgnotify.get_notifier().notify_alert`` here. A hook
            failure never fails the write: alerts.jsonl is the source of
            truth, Telegram is best-effort.
    """

    def __init__(
        self,
        path: Path = DEFAULT_ALERTS_PATH,
        notifier: Callable[[Alert], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.notifier = notifier
        self._lock = threading.Lock()

    def write(self, alert: Alert) -> Alert:
        line = alert.to_json_line()
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        # Mirror into the ordinary log so the daily JSONL is self-contained.
        log.warning(
            "alert.emitted",
            alert_level=alert.level.value,
            alert_code=alert.code.value,
            alert_message=alert.message,
            incident_dir=alert.incident_dir,
        )
        if self.notifier is not None:
            try:
                self.notifier(alert)
            except Exception as exc:
                log.warning("alert.notifier_failed", error=type(exc).__name__)
        return alert

"""Heartbeat file for the external watchdog (docs/ops-monitoring.md §6).

A daemon thread -- deliberately independent from the game loop -- atomically
rewrites ``logs/heartbeat.json`` every 10 s (write tmp -> ``os.replace``).
If the loop hangs, the file goes stale and ``python -m bot2048.tgnotify watch``
synthesizes a ``WATCHDOG_TIMEOUT`` alert after 60 s. On a clean exit the
file is removed so a manually stopped bot never trips the watchdog.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import TracebackType
from typing import Self

import structlog

from bot2048.runner.alerts import utc_now_iso

log = structlog.get_logger(__name__)

__all__ = ["DEFAULT_HEARTBEAT_PATH", "HeartbeatWriter"]

DEFAULT_HEARTBEAT_PATH: Path = Path("logs") / "heartbeat.json"


class HeartbeatWriter:
    """Periodic atomic writer of ``heartbeat.json``.

    ``update()`` is cheap and thread-safe -- the game loop calls it whenever
    game_id/ply change; the daemon thread does the actual file I/O.
    """

    def __init__(
        self,
        run_id: str,
        path: Path = DEFAULT_HEARTBEAT_PATH,
        *,
        interval_s: float = 10.0,
        pid: int | None = None,
    ) -> None:
        self.run_id = run_id
        self.path = Path(path)
        self.interval_s = interval_s
        self.pid = pid if pid is not None else os.getpid()
        self._lock = threading.Lock()
        self._game_id: str | None = None
        self._ply: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def update(self, *, game_id: str | None = None, ply: int | None = None) -> None:
        """Set the context reported in the next heartbeat."""
        with self._lock:
            if game_id is not None:
                self._game_id = game_id
            if ply is not None:
                self._ply = ply

    def write_once(self) -> None:
        """One atomic heartbeat write (tmp + ``os.replace``)."""
        with self._lock:
            payload = {
                "ts": utc_now_iso(),
                "run_id": self.run_id,
                "game_id": self._game_id,
                "ply": self._ply,
                "pid": self.pid,
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- thread lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self.write_once()  # immediate first beat, before the loop even starts
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="bot2048-heartbeat", daemon=True
        )
        self._thread.start()
        log.info("heartbeat.started", path=str(self.path), interval_s=self.interval_s)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.write_once()
            except OSError as exc:  # disk hiccup: log, keep beating
                log.warning("heartbeat.write_failed", error=str(exc))

    def stop(self, *, remove: bool = True) -> None:
        """Stop the thread; ``remove=True`` (clean exit) deletes the file."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 5)
            self._thread = None
        if remove:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("heartbeat.remove_failed", error=str(exc))
        log.info("heartbeat.stopped", removed=remove)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # A crash is NOT a clean exit: leave the file so the watchdog can
        # still fire if the crash handler itself wedges. run_session removes
        # it explicitly on the clean path.
        self.stop(remove=exc_type is None)

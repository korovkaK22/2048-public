"""Run lifecycle: safe (re)starts of the live runner (docs/ops-monitoring.md §7).

* every start = a fresh ``run_id`` + a row in ``runs``;
* a pid lock file blocks double starts (a stale lock from a dead pid is
  ignored);
* restart budget: max 6 auto-restarts per sliding hour (counted via
  ``runs.started_at``) -> otherwise a ``RESTART_LOOP`` FATAL alert and the
  bot refuses to start; cooldown >= 60 s between starts (we sleep out the
  remainder);
* open games in the DB are closed as ``end_reason='interrupted'`` -- the
  live loop then re-recognizes the board FROM THE SCREEN and never trusts
  cache/DB (the app may have regenerated the game after a re-login);
* :func:`run_session` is the top-level guard (§9 item 8): any uncaught
  exception becomes an incident bundle + ``CRASH`` FATAL alert + exit(2);
  a clean exit writes ``runs.exit_reason='clean'`` and removes the
  heartbeat file.

Idempotency note: restarts are safe at any moment because every decision in
the live loop starts from a fresh screenshot -- there are no queued taps.
"""

from __future__ import annotations

import os
import secrets
import signal
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from bot2048.device.supervisor import FatalIncident
from bot2048.runner import incidents
from bot2048.runner.alerts import (
    DEFAULT_ALERTS_PATH,
    AlertCode,
    AlertLevel,
    AlertWriter,
)
from bot2048.runner.escalation import policy_for
from bot2048.runner.heartbeat import DEFAULT_HEARTBEAT_PATH, HeartbeatWriter
from bot2048.storage.errors import StorageError
from bot2048.storage.ops_schema import OpsRepository

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_LOCK_PATH",
    "TERMINATE_SIGNALS",
    "LockHeldError",
    "RestartLoopError",
    "RunContext",
    "RunnerLock",
    "enforce_restart_budget",
    "install_terminate_handlers",
    "new_run_id",
    "run_session",
]

#: Signals that mean "stop now, cleanly": launchd/systemd stop, `kill`, a
#: closed ssh session (SIGHUP). SIGHUP does not exist on Windows; SIGTERM is
#: accepted by ``signal.signal`` there but only ever delivered in-process.
TERMINATE_SIGNALS: tuple[signal.Signals, ...] = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)


def _terminate_to_interrupt(signum: int, _frame: object) -> None:
    """Signal handler: surface the stop request as ``KeyboardInterrupt``.

    Python runs signal handlers in the MAIN thread between bytecodes, so the
    raise lands in the live loop exactly like Ctrl-C does -> :func:`run_session`
    maps it to ``exit_reason='killed'`` and removes the heartbeat (no false
    ``WATCHDOG_TIMEOUT`` from the watcher, no ``NULL`` exit_reason row).
    Deliberately does nothing else: no logging inside a signal handler.
    """
    raise KeyboardInterrupt(signal.Signals(signum).name)


def install_terminate_handlers() -> Callable[[], None]:
    """Route :data:`TERMINATE_SIGNALS` to ``KeyboardInterrupt``; return a restorer.

    No-op (restorer does nothing) when called off the main thread -- the
    interpreter only allows handler installation there, and the live loop
    always runs on it.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: dict[signal.Signals, Any] = {}
    for sig in TERMINATE_SIGNALS:
        try:
            previous[sig] = signal.signal(sig, _terminate_to_interrupt)
        except (ValueError, OSError):  # unsupported on this platform/thread
            continue

    def restore() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    return restore

DEFAULT_LOCK_PATH: Path = Path("logs") / "runner.lock"

RESTART_MAX_PER_HOUR: int = 6
RESTART_COOLDOWN_S: float = 60.0


class LockHeldError(RuntimeError):
    """Another live runner is already running (live pid in the lock file)."""


class RestartLoopError(RuntimeError):
    """Hourly restart budget exceeded; manual intervention required."""


def new_run_id(now: datetime | None = None) -> str:
    """``run-<yyyymmdd-hhmmss>-<4hex>`` (§7)."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(2)}"


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check. NEVER uses ``os.kill`` on Windows --
    there ``os.kill(pid, 0)`` TERMINATES the target process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class RunnerLock:
    """Pid lock file: blocks a second runner; stale locks are ignored."""

    def __init__(self, path: Path = DEFAULT_LOCK_PATH) -> None:
        self.path = Path(path)
        self._owned = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                other_pid = int(self.path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                other_pid = -1
            if _pid_alive(other_pid) and other_pid != os.getpid():
                raise LockHeldError(
                    f"runner already active (pid {other_pid}, lock {self.path}); "
                    "stop it first or wait for it to exit"
                )
            log.warning("lock.stale_ignored", path=str(self.path), stale_pid=other_pid)
        self.path.write_text(str(os.getpid()), encoding="ascii")
        self._owned = True

    def release(self) -> None:
        if not self._owned:
            return
        try:
            if self.path.exists() and self.path.read_text(encoding="ascii").strip() == str(
                os.getpid()
            ):
                self.path.unlink()
        except OSError as exc:
            log.warning("lock.release_failed", error=str(exc))
        self._owned = False


def enforce_restart_budget(
    ops: OpsRepository,
    alerts: AlertWriter,
    *,
    run_id: str,
    now: float | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    incidents_root: Path = incidents.DEFAULT_INCIDENTS_ROOT,
) -> None:
    """§7 limits. Over budget -> ``RESTART_LOOP`` FATAL alert + bundle + raise;
    inside the 60 s cooldown -> sleep out the remainder.

    The bundle carries no screenshot (no device is connected yet) but the
    recent run starts, so the restart storm is traceable from the alert alone.
    No ``live_events`` row: the ``runs`` row of this run does not exist yet
    (FK), and the previous runs' rows already tell the story.
    """
    now = now if now is not None else time.time()
    starts = ops.recent_run_starts(now - 3600.0)
    if len(starts) >= RESTART_MAX_PER_HOUR:
        message = (
            f"{len(starts)} runner starts in the last hour "
            f"(limit {RESTART_MAX_PER_HOUR}) -- refusing to start; "
            "manual decision required"
        )
        incidents.IncidentSink(root=incidents_root, alerts=alerts, run_id=run_id).record(
            AlertCode.RESTART_LOOP,
            AlertLevel.FATAL,
            message,
            attempts=len(starts),
            retry_policy={
                **policy_for(AlertCode.RESTART_LOOP).describe(),
                "max_per_hour": RESTART_MAX_PER_HOUR,
                "cooldown_s": RESTART_COOLDOWN_S,
            },
            stage="startup",
            extra={"recent_starts": [round(s, 3) for s in starts]},
        )
        raise RestartLoopError(message)
    if starts:
        wait = RESTART_COOLDOWN_S - (now - max(starts))
        if wait > 0:
            log.info("lifecycle.cooldown", wait_s=round(wait, 1))
            sleep(wait)


@dataclass(slots=True)
class RunContext:
    """Everything a live loop needs from the lifecycle layer."""

    run_id: str
    ops: OpsRepository
    alerts: AlertWriter
    heartbeat: HeartbeatWriter
    ring: incidents.MoveRing


def _end_run_quietly(ops: OpsRepository, run_id: str, reason: str) -> None:
    try:
        ops.end_run(run_id, reason)
    except StorageError as exc:
        log.warning("lifecycle.end_run_failed", reason=reason, error=str(exc))


@contextmanager
def run_session(
    *,
    db_path: Path,
    alerts_path: Path = DEFAULT_ALERTS_PATH,
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    incidents_root: Path = incidents.DEFAULT_INCIDENTS_ROOT,
    args: dict[str, Any] | None = None,
    git_rev: str | None = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> Iterator[RunContext]:
    """Top-level session guard. Use as::

        with run_session(db_path=Path("data/games.db"), args=vars) as ctx:
            ...live loop; raise FatalIncident via Supervisor on FATAL...

    Exit mapping: clean -> ``exit_reason='clean'``; FatalIncident ->
    ``fatal:<CODE>`` + ``SystemExit(2)`` (alert/bundle already written by
    the Supervisor); KeyboardInterrupt -> ``killed``; any other exception ->
    CRASH bundle + FATAL alert + ``fatal:CRASH`` + ``SystemExit(2)``.
    SIGTERM/SIGHUP (launchd stop, ``kill``, dropped ssh) are turned into
    KeyboardInterrupt for the session's lifetime, so they take the ``killed``
    path too instead of leaving a NULL exit_reason and a stale heartbeat.
    """
    run_id = new_run_id()
    alerts = AlertWriter(alerts_path)
    lock = RunnerLock(lock_path)
    restore_signals = install_terminate_handlers()  # SIGTERM/SIGHUP -> 'killed'
    lock.acquire()
    try:
        ops = OpsRepository(db_path)  # startup REQUIRES a working DB (§7 budget)
        try:
            enforce_restart_budget(
                ops, alerts, run_id=run_id, sleep=sleep, incidents_root=incidents_root
            )
            ops.start_run(run_id, os.getpid(), git_rev=git_rev, args=args or {})
            try:
                closed = ops.mark_interrupted_games()
                if closed:
                    log.info("lifecycle.interrupted_games_closed", count=closed)
            except StorageError as exc:
                log.warning("lifecycle.mark_interrupted_failed", error=str(exc))

            heartbeat = HeartbeatWriter(run_id, heartbeat_path)
            heartbeat.start()
            ctx = RunContext(
                run_id=run_id,
                ops=ops,
                alerts=alerts,
                heartbeat=heartbeat,
                ring=incidents.MoveRing(),
            )
            try:
                yield ctx
            except FatalIncident as exc:
                # Bundle + FATAL alert are already on disk (Supervisor.fatal).
                heartbeat.stop(remove=False)  # §6: not a clean exit
                _end_run_quietly(ops, run_id, f"fatal:{exc.alert.code.value}")
                raise SystemExit(2) from exc
            except KeyboardInterrupt:
                heartbeat.stop(remove=True)  # deliberate manual stop
                _end_run_quietly(ops, run_id, "killed")
                raise
            except Exception as exc:
                heartbeat.stop(remove=False)
                # ONE incident path: bundle (FRESH screenshot through the ring's
                # capture hook the live runner installed, last good frame, last
                # moves, traceback) + FATAL alert (-> Telegram with the photo)
                # + live_events row with incident_dir. The run row still exists
                # here, so the DB mirror works; all of it best-effort.
                incidents.IncidentSink(
                    root=incidents_root, alerts=alerts, ops=ops, run_id=run_id, ring=ctx.ring
                ).record(
                    AlertCode.CRASH,
                    AlertLevel.FATAL,
                    f"unhandled {type(exc).__name__}: {exc}",
                    attempts=1,
                    retry_policy=policy_for(AlertCode.CRASH).describe(),
                    stage="session",
                    extra={"args": args or {}, "pid": os.getpid(), "cause": type(exc).__name__},
                    traceback_text=traceback.format_exc(),
                )
                _end_run_quietly(ops, run_id, "fatal:CRASH")
                raise SystemExit(2) from exc
            else:
                heartbeat.stop(remove=True)  # clean exit: no watchdog trigger
                _end_run_quietly(ops, run_id, "clean")
        finally:
            ops.close()
    finally:
        lock.release()
        restore_signals()

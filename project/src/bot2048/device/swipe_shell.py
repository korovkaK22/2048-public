"""Persistent ``adb shell`` for zero-spawn swipe delivery.

Every ``adb shell input swipe ...`` normally forks a fresh ``adb`` process
(~83 ms of pure fork/attach overhead on this host -- confirmed by an early
swipe benchmark, docs/device-ui-notes.md). At the fast pacing profile the
gesture itself is only 80 ms, so that per-call spawn is the single biggest
slice of the ~215 ms ``swipe_ms`` phase.

:class:`PersistentSwipeShell` keeps ONE long-lived ``adb -s <serial> shell``
process open and writes ``input swipe`` lines to its stdin, one per swipe. The
process is spawned once and reused, so the spawn cost is paid a single time for
the whole session instead of once per move.

Blocking semantics are preserved. Each command is followed by an ``echo`` of a
unique marker; because ``input swipe`` blocks the remote shell for the gesture
duration, the marker is printed only *after* the gesture completes. Reading
stdout until the marker therefore returns at exactly the same moment the old
per-call ``adb swipe`` returned -- the caller's board-settle timing is
unchanged; only the ~83 ms spawn is gone.

Reliability: a dead shell (device dropped / adb server bounced) is detected as
EOF/BrokenPipe, transparently reconnected once, and if it still fails the
caller (:class:`bot2048.device.adb.AdbDevice`) falls back to a one-shot
``adb swipe``. The shell is torn down cleanly on :meth:`close` and via an
``atexit`` hook, so no zombie ``adb`` process is left behind.

The command channel here carries only ``input swipe`` gestures whose endpoints
were already safe-zone clamped by :class:`bot2048.device.humanizer.Humanizer`;
this module changes the *delivery* of that gesture, never its geometry.
"""

from __future__ import annotations

import atexit
import contextlib
import subprocess
import threading
from typing import IO

import structlog

log = structlog.get_logger(__name__)


class SwipeShellError(RuntimeError):
    """The persistent swipe shell could not deliver a swipe.

    Raised only after a reconnect attempt has also failed; the caller should
    fall back to a one-shot ``adb swipe`` so a broken shell never drops a move.
    """


class _ShellBroken(Exception):
    """Internal: the live shell died mid-command (EOF / broken pipe)."""


class PersistentSwipeShell:
    """A reusable ``adb -s <serial> shell`` that streams ``input swipe`` lines.

    Not thread-safe across concurrent swipes; the live loop issues swipes
    serially. A lock only guards :meth:`close` racing the swipe path so
    teardown never corrupts an in-flight write.
    """

    #: Default marker-wait watchdog (seconds); overridden from
    #: :attr:`~bot2048.device.config.DeviceConfig.swipe_shell_timeout_s`.
    DEFAULT_READ_TIMEOUT_S: float = 15.0

    def __init__(
        self, serial: str, adb_path: str, read_timeout_s: float = DEFAULT_READ_TIMEOUT_S
    ) -> None:
        self._serial = serial
        self._adb_path = adb_path
        self._read_timeout_s = read_timeout_s
        self._proc: subprocess.Popen[str] | None = None
        self._seq = 0
        self._lock = threading.Lock()
        self._closed = False
        atexit.register(self.close)

    # -- process lifecycle -------------------------------------------------

    def _ensure_proc(self) -> subprocess.Popen[str]:
        """Return a live shell process, spawning one if needed.

        Raises:
            SwipeShellError: the ``adb shell`` process could not be spawned
                (e.g. adb binary missing) -- not retryable, caller falls back.
        """
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return proc
        self._discard()
        try:
            proc = subprocess.Popen(
                [self._adb_path, "-s", self._serial, "shell"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line-buffered
            )
        except OSError as exc:
            raise SwipeShellError(f"cannot spawn persistent adb shell: {exc}") from exc
        self._proc = proc
        log.debug("swipe_shell.spawned", serial=self._serial)
        return proc

    def _discard(self) -> None:
        """Tear down the current shell process without raising."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    # -- swipe delivery ----------------------------------------------------

    def _send(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        """Write one swipe to the live shell and block until the gesture ends.

        Raises:
            _ShellBroken: the shell died mid-command (retryable by reconnect).
            SwipeShellError: the shell could not be spawned at all.
        """
        proc = self._ensure_proc()
        self._seq += 1
        # The marker is split by an empty quoted string ('""') so the LITERAL
        # command text never contains the assembled marker -- only the shell's
        # echo output does. This stays correct even on the (unexpected) day adb
        # allocates a pty and echoes stdin: we never false-match the command.
        token = f"b2048sw{self._seq}"
        marker = f"__{token}__"
        echo_arg = f'__{token[:5]}""{token[5:]}__'
        command = f'input swipe {x1} {y1} {x2} {y2} {duration_ms}; echo {echo_arg}\n'

        stdin: IO[str] | None = proc.stdin
        stdout: IO[str] | None = proc.stdout
        if stdin is None or stdout is None:  # pragma: no cover - PIPE always set
            raise _ShellBroken("shell has no stdin/stdout pipe")
        try:
            stdin.write(command)
            stdin.flush()
        except (OSError, ValueError) as exc:  # broken/closed pipe
            raise _ShellBroken(f"write failed: {exc}") from exc

        # Read the marker on a watchdog thread joined with a timeout. A bare
        # ``readline()`` loop escapes only on the marker or EOF, so a
        # wedged-but-open shell (device frozen mid-``input swipe``, WiFi stall)
        # would block the live loop FOREVER -- ``_ShellBroken`` never fires and
        # the "never drop a move" one-shot fallback never runs. The thread
        # blocks on ``readline`` exactly as before (happy path unchanged: the
        # marker arrives in microseconds and the join returns at once); only a
        # genuine stall hits the timeout, which we convert to ``_ShellBroken``
        # so the reconnect -> one-shot path recovers. On timeout the caller
        # ``_discard``s the shell, closing this pipe -> the parked reader's
        # ``readline`` returns "" and the daemon thread exits (no leak).
        result: dict[str, object] = {}
        reader = threading.Thread(
            target=self._read_until_marker,
            args=(stdout, marker, result),
            daemon=True,
        )
        reader.start()
        reader.join(self._read_timeout_s)
        if reader.is_alive():
            raise _ShellBroken(
                f"no swipe marker within {self._read_timeout_s:g}s "
                "(shell wedged mid-gesture / link stalled)"
            )
        if result.get("ok"):
            return
        raise _ShellBroken(str(result.get("error", "adb shell closed (EOF)")))

    @staticmethod
    def _read_until_marker(
        stdout: IO[str], marker: str, result: dict[str, object]
    ) -> None:
        """Block on ``readline`` until the marker (``ok``) or EOF (``error``).

        Runs on a daemon watchdog thread so :meth:`_send` can bound the wait.
        Preserves the original blocking semantics: the marker prints only after
        ``input swipe`` returns, so a healthy swipe resolves in microseconds.
        """
        try:
            while True:
                line = stdout.readline()
                if line == "":  # EOF -> the shell process is gone
                    result["error"] = "adb shell closed (EOF)"
                    return
                if marker in line:
                    result["ok"] = True
                    return
        except Exception as exc:  # pragma: no cover - pipe torn down mid-read
            result["error"] = f"read failed: {exc}"

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        """Deliver one swipe via the persistent shell, reconnecting once.

        Blocks until the gesture completes (same semantics as one-shot
        ``adb swipe``). On an unrecoverable failure raises
        :class:`SwipeShellError` so the caller can fall back to a one-shot
        ``adb swipe`` for this move; the next call will re-spawn the shell.
        """
        with self._lock:
            if self._closed:
                raise SwipeShellError("persistent swipe shell is closed")
            last: Exception | None = None
            for _ in range(2):  # initial try + one reconnect
                try:
                    self._send(x1, y1, x2, y2, duration_ms)
                    return
                except _ShellBroken as exc:
                    last = exc
                    self._discard()  # force a fresh shell on the next attempt
            raise SwipeShellError(
                f"persistent swipe failed after reconnect: {last}"
            ) from last

    # -- teardown ----------------------------------------------------------

    def close(self) -> None:
        """Terminate the shell process cleanly (idempotent, no zombies)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            proc = self._proc
            if proc is not None and proc.poll() is None and proc.stdin is not None:
                with contextlib.suppress(OSError, ValueError):
                    proc.stdin.write("exit\n")
                    proc.stdin.flush()
            self._discard()
        with contextlib.suppress(Exception):  # unregister is best-effort
            atexit.unregister(self.close)

    def __del__(self) -> None:  # pragma: no cover - GC-time safety net
        with contextlib.suppress(Exception):
            self.close()

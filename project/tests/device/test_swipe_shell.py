"""Persistent swipe shell: zero-spawn delivery, reconnect/fallback, clean close.

All hardware-free. ``subprocess.Popen`` is replaced by a fake process that
records the ``input swipe`` lines written to stdin and echoes the trailing
marker back on stdout (mirroring how the real ``adb shell`` prints the marker
only after ``input swipe`` returns). The :class:`AdbDevice` integration tests
inject a fake swipe shell / fake adbutils backend, so no adb server is touched.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Any

import pytest

from bot2048.device.adb import AdbDevice
from bot2048.device.config import DeviceConfig
from bot2048.device.humanizer import SwipeParams
from bot2048.device.swipe_shell import PersistentSwipeShell, SwipeShellError

# --------------------------------------------------------------------------
# Fake subprocess plumbing
# --------------------------------------------------------------------------


class _FakeStdin:
    def __init__(self, proc: _FakeProc) -> None:
        self._proc = proc
        self.lines: list[str] = []

    def write(self, data: str) -> int:
        if self._proc.write_raises is not None:
            raise self._proc.write_raises
        self.lines.append(data)
        # Emulate the shell: run `echo <arg>` and print the (quote-stripped)
        # argument on stdout -- but only for the marker echo, and only after
        # the (blocking) swipe has "completed".
        if self._proc.echo_enabled and "; echo " in data:
            echo_arg = data.rsplit("; echo ", 1)[1].strip()
            self._proc.stdout.feed(echo_arg.replace('"', "") + "\n")
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStdout:
    def __init__(self, proc: _FakeProc, *, read_delay: float = 0.0, block: bool = False) -> None:
        self._proc = proc
        self._buf: list[str] = []
        self._read_delay = read_delay
        self._block = block
        self._released = threading.Event()

    def feed(self, line: str) -> None:
        self._buf.append(line)

    def readline(self) -> str:
        if self._buf:
            if self._read_delay:  # slow-but-legit delivery
                time.sleep(self._read_delay)
            return self._buf.pop(0)
        if self._block:
            # A WEDGED-but-open shell: a live readline would block forever. Park
            # until the shell is discarded (close/terminate/kill) -> then EOF, so
            # the watchdog thread this stands in for can exit without leaking.
            self._released.wait()
            return ""
        # Non-blocking fake: EOF so a test never hangs (a live shell would block).
        return ""

    def release(self) -> None:
        self._released.set()

    def close(self) -> None:
        self._released.set()


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` with text pipes."""

    def __init__(
        self,
        argv: list[str],
        *,
        write_raises: Exception | None = None,
        echo_enabled: bool = True,
        read_delay: float = 0.0,
        block: bool = False,
    ) -> None:
        self.argv = argv
        self.write_raises = write_raises
        self.echo_enabled = echo_enabled
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout(self, read_delay=read_delay, block=block)
        self._alive = True
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False
        self.stdout.release()  # unblock a parked reader so it can exit

    def wait(self, timeout: float | None = None) -> int:
        self._alive = False
        return 0

    def kill(self) -> None:
        self.killed = True
        self._alive = False
        self.stdout.release()

    def die(self) -> None:
        """Simulate the shell process exiting (device dropped)."""
        self._alive = False
        self.stdout._buf.clear()


class _PopenFactory:
    """Hands out pre-seeded fake procs and counts spawns."""

    def __init__(self, procs: list[_FakeProc]) -> None:
        self._procs = procs
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> _FakeProc:
        self.calls.append(argv)
        idx = len(self.calls) - 1
        if idx < len(self._procs):
            proc = self._procs[idx]
            proc.argv = argv
            return proc
        return _FakeProc(argv)


@pytest.fixture(autouse=True)
def _no_atexit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real atexit registry clean during tests."""
    monkeypatch.setattr("bot2048.device.swipe_shell.atexit.register", lambda f: f)
    monkeypatch.setattr("bot2048.device.swipe_shell.atexit.unregister", lambda f: None)


# --------------------------------------------------------------------------
# (a) a swipe writes ONE line to the persistent shell -- no per-swipe spawn
# --------------------------------------------------------------------------


def test_swipe_writes_line_and_reuses_single_process(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc([])
    factory = _PopenFactory([proc])
    monkeypatch.setattr(subprocess, "Popen", factory)

    shell = PersistentSwipeShell(serial="ABC123", adb_path="/fake/adb")
    for _ in range(5):
        shell.swipe(100, 200, 300, 400, 80)

    # Exactly one process was spawned for all five swipes (spawn cost paid once).
    assert len(factory.calls) == 1
    assert factory.calls[0] == ["/fake/adb", "-s", "ABC123", "shell"]
    # Each swipe wrote exactly one `input swipe` line to the live shell's stdin.
    swipe_lines = [ln for ln in proc.stdin.lines if ln.startswith("input swipe ")]
    assert len(swipe_lines) == 5
    assert swipe_lines[0].startswith("input swipe 100 200 300 400 80;")


def test_marker_is_not_matched_by_command_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pty that echoes stdin must not be mistaken for the completion marker."""
    proc = _FakeProc([])
    monkeypatch.setattr(subprocess, "Popen", _PopenFactory([proc]))
    shell = PersistentSwipeShell(serial="S", adb_path="/fake/adb")
    shell.swipe(1, 2, 3, 4, 80)
    line = proc.stdin.lines[0]
    # The literal command text carries the split marker ('""'), never the
    # assembled marker the read loop waits for.
    assert '""' in line
    assert "__b2048sw1__" not in line


# --------------------------------------------------------------------------
# (b) dead shell -> reconnect, and unrecoverable -> SwipeShellError (fallback)
# --------------------------------------------------------------------------


def test_dead_shell_reconnects_and_spawns_fresh_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead = _FakeProc([])
    fresh = _FakeProc([])
    factory = _PopenFactory([dead, fresh])
    monkeypatch.setattr(subprocess, "Popen", factory)

    shell = PersistentSwipeShell(serial="S", adb_path="/fake/adb")
    shell.swipe(1, 2, 3, 4, 80)  # spawns `dead`, succeeds
    assert len(factory.calls) == 1

    dead.die()  # device drops between moves -> next write hits EOF
    shell.swipe(5, 6, 7, 8, 80)  # transparently reconnects onto `fresh`

    assert len(factory.calls) == 2  # exactly one reconnect
    assert [ln for ln in fresh.stdin.lines if ln.startswith("input swipe ")]


def test_eof_on_read_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shell alive per poll() but stdout hits EOF mid-read -> reconnect."""
    silent = _FakeProc([], echo_enabled=False)  # accepts write, never echoes
    good = _FakeProc([])
    factory = _PopenFactory([silent, good])
    monkeypatch.setattr(subprocess, "Popen", factory)

    shell = PersistentSwipeShell(serial="S", adb_path="/fake/adb")
    shell.swipe(1, 2, 3, 4, 80)  # readline() -> "" (EOF) -> reconnect onto `good`

    assert len(factory.calls) == 2
    assert [ln for ln in good.stdin.lines if ln.startswith("input swipe ")]


def test_persistent_write_failure_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = _FakeProc([], write_raises=BrokenPipeError("pipe gone"))
    good = _FakeProc([])
    factory = _PopenFactory([broken, good])
    monkeypatch.setattr(subprocess, "Popen", factory)

    shell = PersistentSwipeShell(serial="S", adb_path="/fake/adb")
    shell.swipe(1, 2, 3, 4, 80)  # first write raises -> reconnect onto `good`

    assert len(factory.calls) == 2
    assert [ln for ln in good.stdin.lines if ln.startswith("input swipe ")]


# --------------------------------------------------------------------------
# HANG-PREVENTION #3: a wedged-but-open shell times out instead of blocking
# forever, so the reconnect -> one-shot fallback still runs ("never drop a
# move"). A slow-but-legit gesture under the bound must NOT fire.
# --------------------------------------------------------------------------


def test_wedged_shell_times_out_and_raises_swipe_shell_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # (a) genuine hang: both the initial shell and its reconnect wedge on
    # readline (device frozen mid-gesture). The marker never arrives, so each
    # _send hits the watchdog and the swipe finally raises SwipeShellError --
    # the signal the caller uses to fall back to a one-shot adb swipe.
    wedged1 = _FakeProc([], echo_enabled=False, block=True)
    wedged2 = _FakeProc([], echo_enabled=False, block=True)
    factory = _PopenFactory([wedged1, wedged2])
    monkeypatch.setattr(subprocess, "Popen", factory)

    shell = PersistentSwipeShell(serial="S", adb_path="/fake/adb", read_timeout_s=0.15)
    start = time.monotonic()
    with pytest.raises(SwipeShellError):
        shell.swipe(1, 2, 3, 4, 80)
    # Bounded, not infinite: two watchdog waits (~2 x 0.15 s), never a hang.
    assert time.monotonic() - start < 5.0
    assert len(factory.calls) == 2  # tried, reconnected, then gave up
    # The discarded shells released their parked readers (no leaked block).
    assert wedged1.terminated or wedged1.killed


def test_slow_but_legit_gesture_under_bound_does_not_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # (b) the marker arrives after a real (small) delay, well UNDER the bound:
    # the watchdog must NOT fire -- one process, no reconnect, swipe delivered.
    proc = _FakeProc([], read_delay=0.05)
    factory = _PopenFactory([proc])
    monkeypatch.setattr(subprocess, "Popen", factory)

    shell = PersistentSwipeShell(serial="S", adb_path="/fake/adb", read_timeout_s=1.0)
    shell.swipe(1, 2, 3, 4, 80)  # resolves on the delayed marker, no raise

    assert len(factory.calls) == 1  # no spurious reconnect
    assert [ln for ln in proc.stdin.lines if ln.startswith("input swipe ")]


def test_default_read_timeout_matches_config_floor() -> None:
    # The shipped default is the 15 s floor: vast headroom over a ~0.1 s swipe,
    # so a healthy-but-slow gesture can never trip it.
    from bot2048.device.config import MIN_ADB_COMMAND_TIMEOUT_S

    assert PersistentSwipeShell.DEFAULT_READ_TIMEOUT_S == MIN_ADB_COMMAND_TIMEOUT_S
    assert DeviceConfig().swipe_shell_timeout_s == MIN_ADB_COMMAND_TIMEOUT_S


def test_unrecoverable_shell_raises_swipe_shell_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every spawned proc dies on write -> both the try and the reconnect fail.
    factory = _PopenFactory(
        [
            _FakeProc([], write_raises=BrokenPipeError("x")),
            _FakeProc([], write_raises=BrokenPipeError("x")),
        ]
    )
    monkeypatch.setattr(subprocess, "Popen", factory)
    shell = PersistentSwipeShell(serial="S", adb_path="/fake/adb")
    with pytest.raises(SwipeShellError):
        shell.swipe(1, 2, 3, 4, 80)


def test_spawn_failure_raises_swipe_shell_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(argv: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("no adb here")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    shell = PersistentSwipeShell(serial="S", adb_path="/nope/adb")
    with pytest.raises(SwipeShellError):
        shell.swipe(1, 2, 3, 4, 80)


# --------------------------------------------------------------------------
# (c) clean close -- terminates the process, idempotent, no zombie
# --------------------------------------------------------------------------


def test_close_terminates_process_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc([])
    monkeypatch.setattr(subprocess, "Popen", _PopenFactory([proc]))
    shell = PersistentSwipeShell(serial="S", adb_path="/fake/adb")
    shell.swipe(1, 2, 3, 4, 80)

    shell.close()
    assert proc.poll() is not None  # process is gone (no zombie)
    assert "exit\n" in proc.stdin.lines  # graceful exit was attempted

    shell.close()  # idempotent -- no raise, no double-terminate side effects

    with pytest.raises(SwipeShellError):
        shell.swipe(1, 2, 3, 4, 80)  # closed shell refuses further swipes


# --------------------------------------------------------------------------
# AdbDevice integration: persistent path used by default, one-shot fallback
# --------------------------------------------------------------------------


class _FakeSwipeShell:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[int, int, int, int, int]] = []
        self.closed = False
        self._fail = fail

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.calls.append((x1, y1, x2, y2, duration_ms))
        if self._fail:
            raise SwipeShellError("boom")

    def close(self) -> None:
        self.closed = True


class _FakeAdbBackend:
    def __init__(self) -> None:
        self.oneshot: list[tuple[int, int, int, int, float]] = []
        self.timeouts: list[float | None] = []

    def shell(
        self, cmdargs: object, *, timeout: float | None = None, encoding: str | None = "utf-8"
    ) -> str:
        # The one-shot fallback swipe is now a timed shell call (not adbutils
        # `.swipe`), so it too is bounded against a half-open link.
        self.timeouts.append(timeout)
        assert isinstance(cmdargs, list)
        assert cmdargs[:2] == ["input", "swipe"]
        x1, y1, x2, y2, dur_ms = (int(v) for v in cmdargs[2:])
        self.oneshot.append((x1, y1, x2, y2, dur_ms / 1000.0))
        return ""


def test_adbdevice_uses_persistent_shell_when_present() -> None:
    device = AdbDevice(DeviceConfig())
    backend = _FakeAdbBackend()
    device._device = backend
    shell = _FakeSwipeShell()
    device._swipe_shell = shell  # skip real serial/adb resolution

    device.swipe(SwipeParams(x1=1, y1=2, x2=3, y2=4, duration_ms=80))

    assert shell.calls == [(1, 2, 3, 4, 80)]  # delivered via persistent shell
    assert backend.oneshot == []  # NO per-swipe process spawn


def test_adbdevice_falls_back_to_oneshot_on_shell_error() -> None:
    device = AdbDevice(DeviceConfig())
    backend = _FakeAdbBackend()
    device._device = backend
    device._swipe_shell = _FakeSwipeShell(fail=True)

    device.swipe(SwipeParams(x1=1, y1=2, x2=3, y2=4, duration_ms=80))

    # Persistent delivery failed -> the move still went out one-shot.
    assert backend.oneshot == [(1, 2, 3, 4, 0.08)]


def test_adbdevice_oneshot_when_persistent_disabled() -> None:
    device = AdbDevice(DeviceConfig(persistent_swipe_shell=False))
    backend = _FakeAdbBackend()
    device._device = backend

    device.swipe(SwipeParams(x1=1, y1=2, x2=3, y2=4, duration_ms=80))

    assert device._swipe_shell is None
    assert backend.oneshot == [(1, 2, 3, 4, 0.08)]


def test_adbdevice_disables_shell_when_setup_fails() -> None:
    # A fake backend has no `.serial`; persistent setup must degrade to one-shot
    # once and never retry (so injected/no-device backends keep working).
    device = AdbDevice(DeviceConfig())
    backend = _FakeAdbBackend()
    device._device = backend

    device.swipe(SwipeParams(x1=1, y1=2, x2=3, y2=4, duration_ms=80))

    assert device._swipe_shell_disabled is True
    assert backend.oneshot == [(1, 2, 3, 4, 0.08)]


def test_adbdevice_close_releases_shell() -> None:
    device = AdbDevice(DeviceConfig())
    shell = _FakeSwipeShell()
    device._swipe_shell = shell

    device.close()
    assert shell.closed is True
    assert device._swipe_shell is None
    device.close()  # idempotent

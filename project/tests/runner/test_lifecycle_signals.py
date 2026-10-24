"""SIGTERM/SIGHUP -> KeyboardInterrupt -> ``exit_reason='killed'`` (audit C9).

Before this, a launchd stop / ``kill`` / dropped ssh session killed the runner
mid-move with no Python-level exception: the ``runs`` row kept a NULL
``exit_reason`` and the heartbeat file stayed on disk, so the Telegram watcher
raised a false ``WATCHDOG_TIMEOUT``. The handler is tiny, so the tests pin its
three contracts: it raises KeyboardInterrupt, ``run_session`` installs and
restores it, and a real signal delivered to a session takes the ``killed`` path.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import bot2048
from bot2048.runner import lifecycle
from bot2048.runner.lifecycle import TERMINATE_SIGNALS, install_terminate_handlers, run_session


def _exit_reason(db: Path) -> str | None:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT exit_reason FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return None if row is None else row[0]


def _session_paths(root: Path) -> dict[str, Path]:
    return {
        "db_path": root / "games.db",
        "alerts_path": root / "alerts.jsonl",
        "heartbeat_path": root / "heartbeat.json",
        "lock_path": root / "runner.lock",
        "incidents_root": root / "incidents",
    }


def test_handler_raises_keyboard_interrupt_named_after_signal() -> None:
    with pytest.raises(KeyboardInterrupt, match="SIGTERM"):
        lifecycle._terminate_to_interrupt(int(signal.SIGTERM), None)


def test_install_then_restore_round_trips_previous_handlers() -> None:
    assert TERMINATE_SIGNALS, "SIGTERM must exist on every supported platform"
    before = {sig: signal.getsignal(sig) for sig in TERMINATE_SIGNALS}
    restore = install_terminate_handlers()
    try:
        for sig in TERMINATE_SIGNALS:
            assert signal.getsignal(sig) is lifecycle._terminate_to_interrupt
    finally:
        restore()
    for sig in TERMINATE_SIGNALS:
        assert signal.getsignal(sig) == before[sig]


def test_sigterm_inside_session_is_recorded_as_killed(tmp_path: Path) -> None:
    """In-process delivery (``raise_signal`` works on Windows too): the session
    ends with ``exit_reason='killed'``, the heartbeat is removed, the lock is
    released and the previous SIGTERM handler is back in place."""
    paths = _session_paths(tmp_path)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt), run_session(**paths):
        assert signal.getsignal(signal.SIGTERM) is lifecycle._terminate_to_interrupt
        signal.raise_signal(signal.SIGTERM)
        time.sleep(0.5)  # the handler runs between bytecodes; never reached
        pytest.fail("SIGTERM did not interrupt the session body")
    assert _exit_reason(paths["db_path"]) == "killed"
    assert not paths["heartbeat_path"].exists()
    assert not paths["lock_path"].exists()
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Popen.terminate() is TerminateProcess on Windows, not SIGTERM",
)
def test_external_sigterm_to_subprocess_runner_marks_run_killed(tmp_path: Path) -> None:
    """The launchd/ssh case: a REAL SIGTERM from outside the process."""
    paths = _session_paths(tmp_path)
    ready = tmp_path / "ready"
    child = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        from bot2048.runner.lifecycle import run_session
        root = Path({str(tmp_path)!r})
        with run_session(
            db_path=root / "games.db", alerts_path=root / "alerts.jsonl",
            heartbeat_path=root / "heartbeat.json", lock_path=root / "runner.lock",
            incidents_root=root / "incidents",
        ):
            (root / "ready").touch()
            while True:
                time.sleep(0.05)
        """
    )
    env = dict(os.environ, PYTHONPATH=str(Path(bot2048.__file__).resolve().parents[1]))
    proc = subprocess.Popen([sys.executable, "-c", child], env=env)
    try:
        deadline = time.monotonic() + 30.0
        while not ready.exists():
            if proc.poll() is not None:
                pytest.fail(f"child exited early with {proc.returncode}")
            if time.monotonic() > deadline:
                pytest.fail("child never entered the session")
            time.sleep(0.05)
        proc.terminate()  # SIGTERM
        proc.wait(timeout=20.0)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert _exit_reason(paths["db_path"]) == "killed"
    assert not paths["heartbeat_path"].exists()
    assert not paths["lock_path"].exists()

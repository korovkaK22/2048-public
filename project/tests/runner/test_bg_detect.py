"""Unit tests for the off-loop screen-detect worker (:mod:`bot2048.runner.bg_detect`).

All deterministic: the ``probe`` callable is a test double whose entry/exit are
gated by :class:`threading.Event`, so the worker's publish/consume, standby, and
bounded-join contracts are asserted without a device or wall-clock sleeps racing
the assertions.
"""

from __future__ import annotations

import itertools
import threading
import time

from bot2048.runner.bg_detect import BackgroundStateProbe
from bot2048.runner.escalation import ProbeResult
from bot2048.vision.states import ScreenState

_HEALTHY = ProbeResult(ScreenState.GAME, None, None, 1.0, "healthy game")
_GAMEOVER = ProbeResult(ScreenState.GAME_OVER_POPUP, None, None, 0.0, "game over popup")


def _drain_alert(worker: BackgroundStateProbe, timeout: float = 2.0) -> ProbeResult | None:
    """Poll until an alert appears (bounded); the worker republishes each tick."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alert = worker.poll_alert()
        if alert is not None:
            return alert
        time.sleep(0.005)
    return None


def test_unhealthy_probe_becomes_pending_alert_consumed_once() -> None:
    # First probe is unhealthy (game-over), every later one is healthy: the
    # alert is published once and, once consumed, never reappears.
    counter = itertools.count()
    entered = threading.Event()

    def probe() -> ProbeResult:
        n = next(counter)
        entered.set()
        return _GAMEOVER if n == 0 else _HEALTHY

    worker = BackgroundStateProbe(probe, interval_s=0.005)
    worker.start()
    try:
        assert entered.wait(2.0)
        alert = _drain_alert(worker)
        assert alert is _GAMEOVER
        # Consumed: healthy probes keep running but publish nothing.
        time.sleep(0.05)
        assert worker.poll_alert() is None
    finally:
        worker.close()


def test_healthy_probe_never_alerts() -> None:
    ticks = itertools.count()
    entered = threading.Event()

    def probe() -> ProbeResult:
        next(ticks)
        entered.set()
        return _HEALTHY

    worker = BackgroundStateProbe(probe, interval_s=0.005)
    worker.start()
    try:
        assert entered.wait(2.0)
        time.sleep(0.05)  # let several probes run
        assert worker.poll_alert() is None
    finally:
        worker.close()


def test_poll_alert_never_blocks_on_a_slow_probe() -> None:
    # The probe is stuck mid-"screencap"; poll_alert must still return at once
    # (it only reads the published slot, never waits for the in-flight probe).
    entered = threading.Event()
    release = threading.Event()

    def probe() -> ProbeResult:
        entered.set()
        release.wait(5.0)
        return _GAMEOVER

    worker = BackgroundStateProbe(probe, interval_s=0.0)
    worker.start()
    try:
        assert entered.wait(2.0)  # worker is now blocked inside the probe
        t0 = time.monotonic()
        got = worker.poll_alert()
        assert time.monotonic() - t0 < 0.2  # returned immediately
        assert got is None  # the slow probe has not published yet
        release.set()
        assert _drain_alert(worker) is _GAMEOVER  # publishes once it finishes
    finally:
        release.set()
        worker.close()


def test_standby_stops_probing_and_drops_pending() -> None:
    calls = itertools.count()
    count_lock = threading.Lock()
    seen = {"n": 0}
    entered = threading.Event()

    def probe() -> ProbeResult:
        next(calls)
        with count_lock:
            seen["n"] += 1
        entered.set()
        return _GAMEOVER  # always unhealthy -> would keep alerting if active

    worker = BackgroundStateProbe(probe, interval_s=0.005)
    worker.start()
    try:
        assert entered.wait(2.0)
        worker.note_active(False)  # stand down + drop pending
        time.sleep(0.03)  # let any in-flight probe settle
        with count_lock:
            frozen = seen["n"]
        assert worker.poll_alert() is None  # standby cleared the pending alert
        time.sleep(0.05)  # no probing happens while stood down
        with count_lock:
            assert seen["n"] == frozen
        worker.note_active(True)  # resume
        time.sleep(0.05)
        with count_lock:
            assert seen["n"] > frozen  # probing resumed
    finally:
        worker.close()


def test_close_joins_cleanly_when_idle() -> None:
    def probe() -> ProbeResult:
        return _HEALTHY

    worker = BackgroundStateProbe(probe, interval_s=0.02)
    worker.start()
    assert worker.is_running
    worker.close()
    assert not worker.is_running  # thread joined, no lingering worker


def test_close_returns_promptly_even_if_probe_is_wedged() -> None:
    # A probe blocked forever (dead adb) must not wedge shutdown: close() waits
    # at most join_timeout_s, warns, and returns (the daemon thread is reaped).
    entered = threading.Event()
    release = threading.Event()

    def probe() -> ProbeResult:
        entered.set()
        release.wait(10.0)
        return _HEALTHY

    worker = BackgroundStateProbe(probe, interval_s=0.0, join_timeout_s=0.2)
    worker.start()
    try:
        assert entered.wait(2.0)  # wedged inside the probe
        t0 = time.monotonic()
        worker.close()
        assert time.monotonic() - t0 < 2.0  # did not block for the full 10 s
    finally:
        release.set()  # let the daemon finish

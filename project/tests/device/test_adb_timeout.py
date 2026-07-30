"""HANG-PREVENTION #1: every blocking adb call is bounded by a real timeout.

A half-open WiFi-ADB link makes ``screencap`` / ``swipe`` / ``ui_dump`` / ``tap``
block FOREVER, and the heartbeat watchdog cannot see a loop hang -- so the run
dies as a silent freeze. :meth:`AdbDevice._shell` threads
``DeviceConfig.adb_command_timeout_s`` into the adbutils socket read; on expiry
adbutils raises ``AdbTimeout`` (an ``AdbError``), which the device layer maps to
:class:`DeviceError` so the EXISTING ``DEVICE_LOST`` -> reconnect ladder recovers.

For every path we prove BOTH:
* (a) a genuine hang (adbutils ``AdbTimeout``) surfaces as :class:`DeviceError`
  (the recoverable type), never an unbounded block; and
* (b) a slow-but-legitimate call that returns in time does NOT fire -- and the
  timeout threaded to the socket is the full 20 s, vast headroom above the <=9 s
  endgame search budget, so it can never abort a legit move spuriously.

All hardware-free: a fake backend is injected as ``AdbDevice._device``.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import pytest

from bot2048.device.adb import AdbDevice, DeviceError
from bot2048.device.config import DEFAULT_SCREEN_SIZE, DeviceConfig
from bot2048.device.humanizer import SwipeParams

# adbutils is a device extra; skip cleanly if it is somehow absent (it is what
# raises the timeout we map, so a faithful test needs the real error class).
_errors = pytest.importorskip("adbutils.errors")
AdbTimeout = _errors.AdbTimeout

HIER_XML = '<hierarchy rotation="0"><node text="x"/></hierarchy>'


def _valid_png_bytes() -> bytes:
    """A decodable PNG at the reference device size (screenshot size gate)."""
    from PIL import Image

    w, h = DEFAULT_SCREEN_SIZE
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


class _StallBackend:
    """Every ``shell`` call raises ``AdbTimeout`` -- the half-open-link hang."""

    serial = "FAKE"

    def __init__(self) -> None:
        self.calls = 0

    def shell(
        self, cmdargs: Any, *, timeout: float | None = None, encoding: str | None = "utf-8"
    ) -> Any:
        self.calls += 1
        raise AdbTimeout(f"socket read timed out after {timeout}s")


class _OkBackend:
    """Every ``shell`` call returns promptly; records the timeout it was given."""

    serial = "FAKE"

    def __init__(self, png: bytes) -> None:
        self.timeouts: list[float | None] = []
        self._png = png

    def shell(
        self, cmdargs: Any, *, timeout: float | None = None, encoding: str | None = "utf-8"
    ) -> Any:
        self.timeouts.append(timeout)
        if cmdargs == "screencap -p":
            return self._png
        if isinstance(cmdargs, str) and cmdargs.startswith("cat "):
            return HIER_XML
        return ""  # uiautomator dump / rm / input swipe|tap


def _oneshot_device(backend: Any) -> AdbDevice:
    # Force the one-shot swipe path (persistent shell is fix #3's own test).
    device = AdbDevice(DeviceConfig(persistent_swipe_shell=False))
    device._device = backend
    return device


# -- (a) a genuine hang becomes a recoverable DeviceError --------------------


def test_swipe_oneshot_timeout_raises_device_lost() -> None:
    device = _oneshot_device(_StallBackend())
    with pytest.raises(DeviceError, match="timed out"):
        device.swipe(SwipeParams(x1=1, y1=2, x2=3, y2=4, duration_ms=80))


def test_ui_dump_timeout_raises_device_lost() -> None:
    device = _oneshot_device(_StallBackend())
    with pytest.raises(DeviceError, match="timed out"):
        device.ui_dump()


def test_tap_timeout_raises_device_lost() -> None:
    device = _oneshot_device(_StallBackend())
    with pytest.raises(DeviceError, match="timed out"):
        device.tap(500, 1200)


def test_screenshot_timeout_raises_device_error() -> None:
    # screencap retries then gives up -- a stalled link ends in DeviceError,
    # never an infinite block. One attempt, no inter-attempt sleep.
    device = AdbDevice(
        DeviceConfig(persistent_swipe_shell=False, screencap_retries=1, screencap_retry_delay_s=0.0)
    )
    device._device = _StallBackend()
    with pytest.raises(DeviceError):
        device.screenshot()


# -- (b) a slow-but-legit call does NOT fire; the bound is 20 s (>> 9 s) -----


def test_default_bound_sits_far_above_endgame_budget() -> None:
    cfg = DeviceConfig()
    assert cfg.adb_command_timeout_s == 20.0
    # The endgame search budget is <= 9 s (escalatable to ~15-20 s); 20 s is
    # safely above, so a legit slow move can never trip the socket timeout.
    assert cfg.adb_command_timeout_s > 9.0


def test_legit_swipe_passes_full_timeout_and_succeeds() -> None:
    backend = _OkBackend(_valid_png_bytes())
    device = _oneshot_device(backend)
    device.swipe(SwipeParams(x1=1, y1=2, x2=3, y2=4, duration_ms=80))  # returns cleanly
    assert backend.timeouts == [20.0]  # the full 20 s bound reached the wire


def test_legit_ui_dump_passes_full_timeout_and_succeeds() -> None:
    backend = _OkBackend(_valid_png_bytes())
    device = _oneshot_device(backend)
    xml = device.ui_dump()
    assert "<hierarchy" in xml
    assert set(backend.timeouts) == {20.0}  # dump + cat + rm all bounded at 20 s


def test_legit_screenshot_passes_full_timeout_and_succeeds() -> None:
    backend = _OkBackend(_valid_png_bytes())
    device = _oneshot_device(backend)
    frame = device.screenshot()
    assert frame.shape == (DEFAULT_SCREEN_SIZE[1], DEFAULT_SCREEN_SIZE[0], 3)
    assert backend.timeouts == [20.0]


def test_legit_tap_passes_full_timeout_and_succeeds() -> None:
    backend = _OkBackend(_valid_png_bytes())
    device = _oneshot_device(backend)
    device.tap(500, 1200)
    assert backend.timeouts == [20.0]


# -- config guard: a too-small (spurious-prone) bound is refused outright -----


def test_config_rejects_bound_below_floor() -> None:
    with pytest.raises(ValueError, match="adb_command_timeout_s"):
        DeviceConfig(adb_command_timeout_s=10.0)  # below the 15 s floor


def test_config_rejects_swipe_shell_bound_below_floor() -> None:
    with pytest.raises(ValueError, match="swipe_shell_timeout_s"):
        DeviceConfig(swipe_shell_timeout_s=5.0)

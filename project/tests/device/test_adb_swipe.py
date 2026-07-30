"""Pacing reaches the wire: configured swipe duration -> ``adb swipe`` (mock adb).

No device, no adbutils: a fake backend is injected as ``AdbDevice._device`` and
records the raw ``shell(["input", "swipe", ...], timeout=...)`` calls the one-shot
path now issues, so we can assert the humanizer's configured duration is exactly
what hits the adb call for each pacing profile -- AND that every call carries the
:attr:`DeviceConfig.adb_command_timeout_s` socket bound (the hang guard).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from bot2048.core.types import Direction
from bot2048.device.adb import AdbDevice
from bot2048.device.humanizer import (
    FAST_SWIPE_DURATION_MS,
    Humanizer,
    HumanizerConfig,
    SwipeParams,
)

BBOX = (60, 700, 1020, 1660)


@dataclass
class _SwipeCall:
    x1: int
    y1: int
    x2: int
    y2: int
    duration_s: float


class FakeAdbBackend:
    """Records the adbutils-level ``shell`` swipe calls AdbDevice issues.

    The one-shot swipe now goes out as a timed ``shell(["input", "swipe", ...])``
    (identical wire command to ``adbutils.swipe``) so the fallback carries the
    same socket timeout as every other call; this fake parses that command back
    into a :class:`_SwipeCall` and remembers the ``timeout`` it was given.
    """

    def __init__(self) -> None:
        self.swipes: list[_SwipeCall] = []
        self.timeouts: list[float | None] = []

    def shell(
        self, cmdargs: object, *, timeout: float | None = None, encoding: str | None = "utf-8"
    ) -> str:
        self.timeouts.append(timeout)
        assert isinstance(cmdargs, list)
        assert cmdargs[:2] == ["input", "swipe"]
        x1, y1, x2, y2, dur_ms = (int(v) for v in cmdargs[2:])
        self.swipes.append(_SwipeCall(x1, y1, x2, y2, dur_ms / 1000.0))
        return ""


def _device_with_fake() -> tuple[AdbDevice, FakeAdbBackend]:
    device = AdbDevice()
    backend = FakeAdbBackend()
    device._device = backend  # inject the fake adbutils device
    return device, backend


def test_swipe_passes_duration_ms_as_seconds() -> None:
    device, backend = _device_with_fake()
    device.swipe(SwipeParams(x1=1, y1=2, x2=3, y2=4, duration_ms=300))
    assert backend.swipes == [_SwipeCall(1, 2, 3, 4, 0.3)]


def test_fast_profile_duration_reaches_swipe_call() -> None:
    device, backend = _device_with_fake()
    hum = Humanizer(config=HumanizerConfig.for_profile("fast"), rng=random.Random(0))
    expected_s = FAST_SWIPE_DURATION_MS / 1000.0
    for direction in Direction:
        params = device.swipe_direction(direction, BBOX, hum)
        assert params.duration_ms == FAST_SWIPE_DURATION_MS  # short, fixed swipe
    # every executed gesture carried the short fast duration to the adb layer
    assert [c.duration_s for c in backend.swipes] == [expected_s] * 4


def test_human_profile_duration_reaches_swipe_call_within_band() -> None:
    device, backend = _device_with_fake()
    hum = Humanizer(config=HumanizerConfig.for_profile("human"), rng=random.Random(7))
    for _ in range(50):
        params = device.swipe_direction(Direction.UP, BBOX, hum)
        # 300 +/- 45 ms jitter -> 0.255..0.345 s, and the SAME value hits adb
        assert 255 <= params.duration_ms <= 345
        assert backend.swipes[-1].duration_s == pytest.approx(params.duration_ms / 1000.0)


def test_custom_swipe_duration_config_reaches_swipe_call() -> None:
    device, backend = _device_with_fake()
    hum = Humanizer(config=HumanizerConfig(swipe_duration_ms=500, duration_jitter_ms=0))
    device.swipe_direction(Direction.RIGHT, BBOX, hum)
    assert backend.swipes[-1].duration_s == 0.5

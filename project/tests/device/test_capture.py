"""FrameSource seam + DEPRECATED ScreencapSource (fallback & AUTH sensor)."""

from __future__ import annotations

import numpy as np
import pytest
import structlog.testing

from bot2048.device import adb as adb_module
from bot2048.device import capture as capture_module
from bot2048.device.adb import AdbDevice, DeviceError, EmptyScreencapError
from bot2048.device.capture import FrameSource, MinicapConfig, ScreencapSource
from bot2048.device.config import DeviceConfig
from bot2048.device.minicap import MinicapSource


def _frame(h: int = 4, w: int = 4) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


class FakeScreencapDevice:
    """Satisfies the ScreencapDevice protocol; scripted screenshot outcomes."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def screenshot(self) -> np.ndarray:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, np.ndarray)
        return outcome


@pytest.fixture(autouse=True)
def _reset_deprecation_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture_module, "_SCREENCAP_DEPRECATION_WARNED", False)
    monkeypatch.setattr(adb_module, "_SCREENSHOT_PRIMARY_NOTED", False)


def test_screencap_source_returns_device_frame() -> None:
    frame = _frame()
    device = FakeScreencapDevice([frame])
    source = ScreencapSource(device)
    got = source.capture()
    assert got is frame
    assert device.calls == 1


def test_screencap_source_is_context_manager() -> None:
    device = FakeScreencapDevice([_frame()])
    with ScreencapSource(device) as source:
        assert source.capture().shape == (4, 4, 3)


def test_screencap_deprecation_warning_fires_once() -> None:
    with structlog.testing.capture_logs() as logs:
        ScreencapSource(FakeScreencapDevice([]))
        ScreencapSource(FakeScreencapDevice([]))
    events = [e for e in logs if e["event"] == "capture.screencap_source_deprecated"]
    assert len(events) == 1


def test_is_auth_blocked_true_on_stable_empty_screencap() -> None:
    device = FakeScreencapDevice([EmptyScreencapError("0 bytes on every attempt")])
    assert ScreencapSource(device).is_auth_blocked() is True


def test_is_auth_blocked_false_when_frames_flow() -> None:
    device = FakeScreencapDevice([_frame()])
    assert ScreencapSource(device).is_auth_blocked() is False


def test_is_auth_blocked_propagates_non_auth_failures() -> None:
    device = FakeScreencapDevice([DeviceError("device lost")])
    with pytest.raises(DeviceError, match="device lost"):
        ScreencapSource(device).is_auth_blocked()


def test_both_sources_satisfy_frame_source_protocol() -> None:
    screencap = ScreencapSource(FakeScreencapDevice([]))
    minicap = MinicapSource(MinicapConfig())
    assert isinstance(screencap, FrameSource)
    assert isinstance(minicap, FrameSource)


def test_adb_screenshot_logs_primary_note_once() -> None:
    config = DeviceConfig(screencap_retries=1, screencap_retry_delay_s=0.0)
    device = AdbDevice(config)  # never connected: screenshot fails fast, no adb
    with structlog.testing.capture_logs() as logs:
        for _ in range(2):
            with pytest.raises(DeviceError):
                device.screenshot()
    events = [e for e in logs if e["event"] == "adb.screenshot_primary"]
    assert len(events) == 1


def test_minicap_config_defaults_and_validation() -> None:
    config = MinicapConfig()
    assert config.local_port == 1313
    assert config.bin_dir == "/data/local/tmp"
    assert config.virtual_size is None
    assert config.effective_virtual_size == config.real_size  # 1:1, calibration-safe
    with pytest.raises(ValueError, match="local_port"):
        MinicapConfig(local_port=0)
    with pytest.raises(ValueError, match="rotation"):
        MinicapConfig(rotation=45)
    with pytest.raises(ValueError, match="virtual_size"):
        MinicapConfig(virtual_size=(0, 1200))

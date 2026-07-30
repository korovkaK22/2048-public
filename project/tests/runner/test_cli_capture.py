"""_make_capture_device: the ``play --capture`` seam (no adb, no ffmpeg).

Constructing the capture path must be side-effect-free: these tests would
hang/fail loudly if anything spawned adb or ffmpeg.
"""

from __future__ import annotations

import pytest
import typer

from bot2048.device.capture import StreamingCaptureDevice
from bot2048.device.scrcpy import AdbScrcpyRunner, ScrcpySource
from bot2048.runner.cli import _make_capture_device


class _StubAdbDevice:
    """Stored by the wrapper, never called during construction."""


def test_screencap_kind_is_identity() -> None:
    dev = _StubAdbDevice()
    got, owner = _make_capture_device(dev, "screencap", None)  # type: ignore[arg-type]
    assert got is dev
    assert owner is None


def test_scrcpy_kind_wraps_device_without_starting_anything() -> None:
    dev = _StubAdbDevice()
    got, owner = _make_capture_device(dev, "scrcpy", "192.168.1.120:5555")  # type: ignore[arg-type]
    assert isinstance(got, StreamingCaptureDevice)
    assert owner is got  # the CLI closes exactly this wrapper
    assert got._device is dev

    source = got._source
    assert isinstance(source, ScrcpySource)
    assert source.config.device_serial == "192.168.1.120:5555"
    runner = source._runner
    assert isinstance(runner, AdbScrcpyRunner)
    assert runner.serial == "192.168.1.120:5555"

    # NOTHING was started: no adb forward, no server, no ffmpeg.
    assert got._source_started is False
    assert source._started is False
    assert runner._proc is None


def test_unknown_kind_exits() -> None:
    with pytest.raises(typer.Exit):
        _make_capture_device(_StubAdbDevice(), "minicap", None)  # type: ignore[arg-type]

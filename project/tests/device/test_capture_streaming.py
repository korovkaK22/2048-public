"""StreamingCaptureDevice: fast stream path + bulletproof screencap fallback.

Pure unit tests -- fake source + fake device, no adb, no ffmpeg, no sleeps.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

import numpy as np
import pytest
from structlog.testing import capture_logs

from bot2048.core.types import Direction
from bot2048.device.adb import EmptyScreencapError
from bot2048.device.capture import Image, StreamingCaptureDevice
from bot2048.device.scrcpy import ScrcpySecureError


def _bright(value: int = 200) -> Image:
    return np.full((4, 4, 3), value, dtype=np.uint8)


def _black() -> Image:
    return np.zeros((4, 4, 3), dtype=np.uint8)


class FakeSource:
    """Scripted FrameSource: each capture() pops the next outcome (frame or exc)."""

    def __init__(
        self,
        outcomes: list[object] | None = None,
        *,
        start_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.outcomes = list(outcomes or [])
        self.start_error = start_error
        self.close_error = close_error
        self.start_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error

    def capture(self) -> Image:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, np.ndarray)
        return outcome

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error

    def __enter__(self) -> FakeSource:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class FakeDevice:
    """Scripted CaptureDevice: screenshot() pops outcomes; the rest records calls."""

    def __init__(self, outcomes: list[object] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.screenshot_calls = 0
        self.taps: list[tuple[int, int]] = []
        self.wakes = 0
        self.ui_dumps = 0
        self.swipes: list[tuple[Any, Any, Any]] = []
        self.swipe_result = object()  # identity marker for delegation

    def screenshot(self) -> Image:
        self.screenshot_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, np.ndarray)
        return outcome

    def ui_dump(self) -> str:
        self.ui_dumps += 1
        return "<xml/>"

    def tap(self, x: int, y: int, *, sensitive: bool = False) -> None:
        self.taps.append((x, y))

    def wake(self) -> None:
        self.wakes += 1

    def swipe_direction(self, direction: Any, grid_bbox: Any, humanizer: Any) -> Any:
        self.swipes.append((direction, grid_bbox, humanizer))
        return self.swipe_result


def _events(logs: list[MutableMapping[str, Any]]) -> list[str]:
    return [entry["event"] for entry in logs]


def test_healthy_stream_serves_source_frames_and_starts_lazily() -> None:
    f1, f2 = _bright(100), _bright(150)
    source = FakeSource([f1, f2])
    device = FakeDevice()
    wrapper = StreamingCaptureDevice(device, source)
    assert source.start_calls == 0  # construction touches nothing
    assert wrapper.screenshot() is f1
    assert wrapper.screenshot() is f2
    assert source.start_calls == 1  # started lazily, exactly once
    assert device.screenshot_calls == 0  # screencap never used


def test_start_failure_falls_back_then_disables_after_max_failures() -> None:
    source = FakeSource(start_error=OSError("no ffmpeg"))
    frames = [_bright(60 + i) for i in range(5)]
    device = FakeDevice(list(frames))
    wrapper = StreamingCaptureDevice(device, source, max_failures=3)
    with capture_logs() as logs:
        assert wrapper.screenshot() is frames[0]
        assert wrapper.screenshot() is frames[1]
        assert wrapper.screenshot() is frames[2]
    events = _events(logs)
    assert events.count("capture.streaming_fallback") == 2
    assert events.count("capture.streaming_disabled") == 1
    # Disabled: subsequent captures never touch the source again.
    start_calls = source.start_calls
    assert wrapper.screenshot() is frames[3]
    assert wrapper.screenshot() is frames[4]
    assert source.start_calls == start_calls


def test_capture_failure_counter_resets_after_a_good_frame() -> None:
    good1, good2 = _bright(90), _bright(110)
    source = FakeSource([OSError("drop 1"), good1, OSError("drop 2"), OSError("drop 3"), good2])
    device = FakeDevice([_bright(10), _bright(11), _bright(12)])
    wrapper = StreamingCaptureDevice(device, source, max_failures=3)
    with capture_logs() as logs:
        wrapper.screenshot()  # failure #1 -> screencap
        assert wrapper.screenshot() is good1  # success resets the counter
        wrapper.screenshot()  # failure #1 again
        wrapper.screenshot()  # failure #2 (max_failures - 1): still NOT disabled
        assert wrapper.screenshot() is good2  # stream still alive
    events = _events(logs)
    assert "capture.streaming_disabled" not in events
    assert events.count("capture.streaming_fallback") == 3


def test_black_frame_served_from_screencap_without_killing_the_stream() -> None:
    bright = _bright()
    source = FakeSource([_black(), bright])
    dev_frame = _bright(50)
    device = FakeDevice([dev_frame])
    wrapper = StreamingCaptureDevice(device, source)
    with capture_logs() as logs:
        assert wrapper.screenshot() is dev_frame  # black -> one screencap frame
    assert "capture.streaming_black_frame" in _events(logs)
    close_calls = source.close_calls  # only the pre-start safety close
    assert wrapper.screenshot() is bright  # stream kept alive, no restart
    assert source.start_calls == 1
    assert source.close_calls == close_calls
    assert wrapper._failures == 0  # a black frame is NOT a failure


def test_secure_error_holds_on_screencap_until_the_wall_clears() -> None:
    after_wall = _bright(130)
    source = FakeSource([ScrcpySecureError("30+ black frames"), after_wall])
    wall_frame = _bright(20)
    real_frame = _bright(140)
    device = FakeDevice(
        [
            wall_frame,
            EmptyScreencapError("screencap returned 0 bytes"),
            EmptyScreencapError("screencap returned 0 bytes"),
            real_frame,
        ]
    )
    wrapper = StreamingCaptureDevice(device, source)
    with capture_logs() as logs:
        assert wrapper.screenshot() is wall_frame  # secure -> source closed + screencap
    assert "capture.streaming_secure_fallback" in _events(logs)
    assert source.close_calls >= 2  # pre-start safety close + secure close
    start_calls = source.start_calls
    # AUTH sensor intact: EmptyScreencapError PROPAGATES, source stays down.
    for _ in range(2):
        with pytest.raises(EmptyScreencapError):
            wrapper.screenshot()
    assert source.start_calls == start_calls
    # Wall gone: screencap succeeds, hold clears...
    assert wrapper.screenshot() is real_frame
    # ...and the NEXT capture restarts the stream.
    assert wrapper.screenshot() is after_wall
    assert source.start_calls == start_calls + 1


def test_last_capture_kind_names_the_factual_source_of_each_frame() -> None:
    # stream OK -> stream error (fallback, not yet disabled) -> black -> stream OK
    source = FakeSource([_bright(100), OSError("drop"), _black(), _bright(120)])
    device = FakeDevice([_bright(10), _bright(11)])
    wrapper = StreamingCaptureDevice(device, source, max_failures=3)
    assert wrapper.last_capture_kind() is None  # nothing served yet
    wrapper.screenshot()
    assert wrapper.last_capture_kind() == "scrcpy"
    with capture_logs():
        wrapper.screenshot()  # stream failure -> screencap served
        assert wrapper.last_capture_kind() == "screencap"
        wrapper.screenshot()  # black stream frame -> screencap served
        assert wrapper.last_capture_kind() == "screencap"
    wrapper.screenshot()  # healthy stream frame again
    assert wrapper.last_capture_kind() == "scrcpy"


def test_last_capture_kind_is_screencap_once_disabled_or_on_secure_hold() -> None:
    disabled = StreamingCaptureDevice(
        FakeDevice([_bright(1), _bright(2)]),
        FakeSource(start_error=OSError("no ffmpeg")),
        max_failures=1,
    )
    with capture_logs():
        disabled.screenshot()  # first failure disables the stream for good
        disabled.screenshot()
    assert disabled.last_capture_kind() == "screencap"

    held = StreamingCaptureDevice(
        FakeDevice([_bright(3)]), FakeSource([ScrcpySecureError("30+ black frames")])
    )
    with capture_logs():
        held.screenshot()  # secure hold -> screencap (the AUTH sensor path)
    assert held.last_capture_kind() == "screencap"


def test_close_is_idempotent_and_swallows_source_errors() -> None:
    source = FakeSource()
    wrapper = StreamingCaptureDevice(FakeDevice(), source)
    wrapper.close()
    wrapper.close()
    assert source.close_calls == 2

    raising = FakeSource(close_error=OSError("socket already dead"))
    wrapper2 = StreamingCaptureDevice(FakeDevice(), raising)
    with capture_logs() as logs:
        wrapper2.close()  # must not raise
    assert "capture.streaming_close_failed" in _events(logs)


def test_non_screenshot_calls_delegate_verbatim() -> None:
    device = FakeDevice()
    wrapper = StreamingCaptureDevice(device, FakeSource())
    wrapper.tap(10, 20)
    wrapper.wake()
    assert wrapper.ui_dump() == "<xml/>"
    bbox_marker = (1, 2, 3, 4)
    humanizer_marker = object()
    result = wrapper.swipe_direction(Direction.UP, bbox_marker, humanizer_marker)  # type: ignore[arg-type]
    assert result is device.swipe_result
    assert device.taps == [(10, 20)]
    assert device.wakes == 1
    assert device.ui_dumps == 1
    assert device.swipes == [(Direction.UP, bbox_marker, humanizer_marker)]


def test_context_manager_starts_eagerly_and_closes() -> None:
    frame = _bright()
    source = FakeSource([frame])
    with StreamingCaptureDevice(FakeDevice(), source) as wrapper:
        assert source.start_calls == 1  # eager warm-up
        assert wrapper.screenshot() is frame  # no second start
        assert source.start_calls == 1
    assert source.close_calls >= 1


def test_eager_start_failure_never_raises_and_is_counted() -> None:
    source = FakeSource(start_error=OSError("adb forward failed"))
    device = FakeDevice([_bright(30)])
    wrapper = StreamingCaptureDevice(device, source, max_failures=3)
    with capture_logs() as logs:
        wrapper.start()  # must not raise
    assert "capture.streaming_start_failed" in _events(logs)
    assert wrapper._failures == 1
    assert wrapper.screenshot() is not None  # still serves frames (retry then fallback)

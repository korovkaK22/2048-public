"""On-device swipe injection: command encoding, backend fallback, capability probe.

Fully offline: the sampler socket is a scripted fake, the accessibility probe is
a stub, and the ``input swipe`` fallback is a recording double -- so every path
(inject, probe-disabled fallback, link-down fallback, blocking) is deterministic
without adb or a phone.
"""

from __future__ import annotations

import struct
import time

import pytest

from bot2048.device.apk_capture import ApkBoardSource, LinkLike
from bot2048.device.apk_swipe import (
    ACCESSIBILITY_COMPONENT,
    ACCESSIBILITY_SERVICE_SHORT,
    ApkSwipeInjector,
    accessibility_enabled,
)
from bot2048.device.humanizer import SwipeParams
from bot2048.ondevice.host import Message
from bot2048.ondevice.packet import (
    CMD_SWIPE,
    SWIPE_ARG_SIZE,
    PacketError,
    encode_swipe,
)

_PARAMS = SwipeParams(x1=540, y1=1600, x2=540, y2=1000, duration_ms=80)


# -- encode_swipe (wire format) --------------------------------------------


def test_encode_swipe_layout() -> None:
    raw = encode_swipe(540, 1600, 541, 1001, 80)
    assert len(raw) == 1 + SWIPE_ARG_SIZE
    assert raw[0] == CMD_SWIPE
    opcode, x1, y1, x2, y2, dur = struct.unpack(">B5H", raw)
    assert (opcode, x1, y1, x2, y2, dur) == (CMD_SWIPE, 540, 1600, 541, 1001, 80)


@pytest.mark.parametrize(
    "args",
    [
        (-1, 0, 0, 0, 10),
        (0, 0, 0, 0, 0),  # duration must be >= 1
        (70000, 0, 0, 0, 10),  # out of u16 range
        (0, 0, 0, 0, 70000),
    ],
)
def test_encode_swipe_rejects_bad_fields(args: tuple[int, int, int, int, int]) -> None:
    with pytest.raises(PacketError):
        encode_swipe(*args)


# -- ApkSwipeInjector -------------------------------------------------------


class _RecordingFallback:
    def __init__(self) -> None:
        self.calls: list[SwipeParams] = []

    def __call__(self, params: SwipeParams) -> None:
        self.calls.append(params)


def test_injects_when_service_enabled_and_blocks() -> None:
    sent: list[tuple[int, int, int, int, int]] = []
    fallback = _RecordingFallback()
    injector = ApkSwipeInjector(
        send_swipe=lambda *a: (sent.append(a) or True),
        fallback=fallback,
        probe=lambda: True,
    )
    start = time.monotonic()
    injector.swipe(_PARAMS)
    elapsed_ms = (time.monotonic() - start) * 1000.0

    assert sent == [(540, 1600, 540, 1000, 80)]
    assert fallback.calls == []  # injection path, no shell fallback
    assert elapsed_ms >= 70  # blocked ~duration_ms (80) to match input-swipe


def test_falls_back_when_service_disabled_and_probes_once() -> None:
    probes = {"n": 0}

    def probe() -> bool:
        probes["n"] += 1
        return False

    sent: list[object] = []
    fallback = _RecordingFallback()
    injector = ApkSwipeInjector(
        send_swipe=lambda *a: (sent.append(a) or True),
        fallback=fallback,
        probe=probe,
    )
    injector.swipe(_PARAMS)
    injector.swipe(_PARAMS)

    assert sent == []  # never tried injection
    assert len(fallback.calls) == 2  # both moves via shell
    assert probes["n"] == 1  # capability probed exactly once


def test_falls_back_when_send_fails_but_stays_armed() -> None:
    outcomes = iter([False, True])
    sent_attempts = {"n": 0}

    def send(*_a: int) -> bool:
        sent_attempts["n"] += 1
        return next(outcomes)

    fallback = _RecordingFallback()
    injector = ApkSwipeInjector(
        send_swipe=send, fallback=fallback, probe=lambda: True, block=False
    )
    injector.swipe(_PARAMS)  # send fails -> fallback, injection stays armed
    injector.swipe(_PARAMS)  # send succeeds -> no fallback

    assert sent_attempts["n"] == 2  # retried on the second move (not disabled)
    assert len(fallback.calls) == 1  # only the failed move fell back


def test_block_false_skips_the_sleep() -> None:
    injector = ApkSwipeInjector(
        send_swipe=lambda *a: True,
        fallback=_RecordingFallback(),
        probe=lambda: True,
        block=False,
    )
    start = time.monotonic()
    injector.swipe(_PARAMS)
    assert (time.monotonic() - start) * 1000.0 < 50  # no duration sleep


def test_no_probe_assumes_available() -> None:
    sent: list[object] = []
    injector = ApkSwipeInjector(
        send_swipe=lambda *a: (sent.append(a) or True),
        fallback=_RecordingFallback(),
        probe=None,
        block=False,
    )
    injector.swipe(_PARAMS)
    assert sent == [(540, 1600, 540, 1000, 80)]


# -- accessibility_enabled (capability probe over adb) ----------------------


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (f"{ACCESSIBILITY_SERVICE_SHORT}\n".encode(), True),
        (f"{ACCESSIBILITY_COMPONENT}\n".encode(), True),
        (b"com.other/.Service:" + ACCESSIBILITY_COMPONENT.encode(), True),
        (b"null\n", False),
        (b"\n", False),
        (b"com.other/.SomethingElse\n", False),
    ],
)
def test_accessibility_enabled_parsing(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes, expected: bool
) -> None:
    monkeypatch.setattr(
        "bot2048.device.apk_swipe.subprocess.run",
        lambda *a, **k: _FakeCompleted(0, stdout),
    )
    assert accessibility_enabled(adb_exe="adb", serial="XYZ") is expected


def test_accessibility_enabled_false_on_adb_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bot2048.device.apk_swipe.subprocess.run",
        lambda *a, **k: _FakeCompleted(1, b""),
    )
    assert accessibility_enabled() is False


def test_accessibility_enabled_false_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("adb not found")

    monkeypatch.setattr("bot2048.device.apk_swipe.subprocess.run", boom)
    assert accessibility_enabled() is False


# -- ApkBoardSource.send_swipe (writes over the shared sampler link) ---------


class _SwipeFakeLink:
    """A :class:`LinkLike` that records swipe writes and can fail them."""

    def __init__(self, *, fail: bool = False) -> None:
        self.swipes: list[tuple[int, int, int, int, int]] = []
        self.closed = False
        self._fail = fail

    def read_message(self) -> Message | None:
        time.sleep(0.005)
        raise TimeoutError("idle")

    def ping(self) -> None: ...

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        if self._fail:
            raise OSError("socket write failed")
        self.swipes.append((x1, y1, x2, y2, duration_ms))

    def close(self) -> None:
        self.closed = True


def _source_with(link: LinkLike | None) -> ApkBoardSource:
    def connector(port: int, host: str, timeout_s: float) -> LinkLike:
        if link is None:
            raise OSError("refused")
        return link

    return ApkBoardSource(port=27184, connector=connector, forwarder=lambda: True)


def test_send_swipe_success_writes_over_link() -> None:
    link = _SwipeFakeLink()
    src = _source_with(link)
    assert src.send_swipe(540, 1600, 540, 1000, 80) is True
    assert link.swipes == [(540, 1600, 540, 1000, 80)]
    src.close()


def test_send_swipe_false_when_no_link() -> None:
    src = _source_with(None)  # connector always refuses
    assert src.send_swipe(1, 2, 3, 4, 80) is False


def test_send_swipe_drops_link_on_write_error() -> None:
    link = _SwipeFakeLink(fail=True)
    src = _source_with(link)
    assert src.send_swipe(1, 2, 3, 4, 80) is False
    assert link.closed is True  # bad link dropped so the next call reconnects


def test_send_swipe_end_to_end_via_injector() -> None:
    """The injector's send_swipe wired to a real ApkBoardSource link."""
    link = _SwipeFakeLink()
    src = _source_with(link)
    injector = ApkSwipeInjector(
        send_swipe=src.send_swipe,
        fallback=_RecordingFallback(),
        probe=lambda: True,
        block=False,
    )
    injector.swipe(_PARAMS)
    assert link.swipes == [(540, 1600, 540, 1000, 80)]
    src.close()

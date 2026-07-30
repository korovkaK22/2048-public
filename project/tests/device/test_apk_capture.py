"""ApkBoardSource: board packets -> BoardSample, reconnect, forward, flags.

Fully offline: a scripted fake link stands in for the forwarded sampler socket
(no adb, no phone), so every path -- fresh board, frame-skip, timeout lull,
EOF drop + reconnect, flag mapping, forward setup -- is deterministic.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from bot2048.core.types import BOARD_CELLS
from bot2048.device.apk_capture import ApkBoardSource, BoardSample, LinkLike
from bot2048.ondevice.host import Message
from bot2048.ondevice.packet import BoardPacket, FramePacket, FrameReason, PacketFlag

_BOARD = (0, 1, 2, 3, 0, 4, 5, 6, 0, 1, 2, 8, 0, 0, 15, 11)

#: Sentinel a scripted step can yield to make ``read_message`` raise TimeoutError.
_TIMEOUT = object()


def _packet(seq: int = 1, flags: PacketFlag = PacketFlag.NONE, uncertain: int = 0) -> BoardPacket:
    return BoardPacket(board=_BOARD, seq=seq, uncertain=uncertain, flags=flags, device_ts_ms=7)


def _frame(seq: int = 1) -> FramePacket:
    return FramePacket(reason=FrameReason.PERIODIC, seq=seq, payload=b"\xff\xd8jpeg\xff\xd9")


class FakeLink:
    """Scripted :class:`LinkLike`: each ``read_message`` pops one step.

    A step is a ``Message`` (returned), ``None`` (EOF, returned as-is), or the
    :data:`_TIMEOUT` sentinel (raises ``TimeoutError``, i.e. "no packet yet").

    An exhausted script defaults to a clean EOF. Tests about lulls pass
    ``exhausted="timeout"`` instead: the background drain thread reads PAST
    the scripted steps, so for those tests "no more steps" must mean "idle but
    alive socket" (endless recv timeouts), not "the device hung up". The tiny
    sleep paces the drain loop like a real socket timeout would.
    """

    def __init__(self, steps: list[object], *, exhausted: str = "eof") -> None:
        assert exhausted in ("eof", "timeout")
        self._steps = list(steps)
        self._exhausted = exhausted
        self.pings = 0
        self.closed = False

    def read_message(self) -> Message | None:
        if not self._steps:
            if self._exhausted == "timeout":
                time.sleep(0.005)  # pace the drain like a real recv timeout
                raise TimeoutError("no packet yet")
            return None  # exhausted -> behave like a clean EOF
        step = self._steps.pop(0)
        if step is _TIMEOUT:
            raise TimeoutError("no packet yet")
        return step  # type: ignore[return-value]

    def ping(self) -> None:
        self.pings += 1

    def close(self) -> None:
        self.closed = True


def _source(
    *links: FakeLink,
    forwarder: Callable[[], bool] | None = None,
) -> tuple[ApkBoardSource, list[tuple[int, str, float]]]:
    """A source whose connector hands out ``links`` in order; records connect args."""
    queue = list(links)
    calls: list[tuple[int, str, float]] = []

    def connector(port: int, host: str, timeout_s: float) -> LinkLike:
        calls.append((port, host, timeout_s))
        if not queue:
            raise OSError("no more scripted links")
        return queue.pop(0)

    src = ApkBoardSource(
        port=27184,
        connector=connector,
        forwarder=forwarder if forwarder is not None else (lambda: True),
    )
    return src, calls


# -- poll: the happy path + frame skipping ------------------------------------


def test_poll_returns_board_and_seq_from_packet() -> None:
    src, _ = _source(FakeLink([_packet(seq=42, flags=PacketFlag.BOARD_CHANGED)]))
    sample = src.poll()
    assert sample is not None
    assert sample.board == _BOARD
    assert len(sample.board) == BOARD_CELLS
    assert sample.seq == 42
    assert sample.changed is True
    assert sample.trustworthy is True


def test_poll_skips_interleaved_frame_packets() -> None:
    src, _ = _source(FakeLink([_frame(seq=1), _frame(seq=1), _packet(seq=5)]))
    sample = src.poll()
    assert sample is not None
    assert sample.seq == 5


def test_poll_timeout_is_no_board_not_a_drop() -> None:
    # exhausted="timeout": the drain keeps reading past the scripted step, and
    # an idle-but-alive socket must stay a lull forever, never become an EOF.
    link = FakeLink([_TIMEOUT], exhausted="timeout")
    src, _ = _source(link)
    try:
        assert src.poll() is None  # lull, not EOF
        assert link.closed is False  # link kept for the next poll
    finally:
        src.close()  # stop the drain thread; don't leak it into other tests


# -- flag mapping / trustworthiness -------------------------------------------


@pytest.mark.parametrize(
    ("flags", "uncertain", "trustworthy", "field"),
    [
        (PacketFlag.NOT_IN_GAME, 0, False, "not_in_game"),
        (PacketFlag.SECURE_SUSPECTED, 0, False, "secure_suspected"),
        (PacketFlag.HAS_UNCERTAIN, 0, True, "uncertain"),
        (PacketFlag.NONE, 0b101, True, "uncertain"),
    ],
)
def test_flag_mapping(
    flags: PacketFlag, uncertain: int, trustworthy: bool, field: str
) -> None:
    sample = BoardSample.from_packet(_packet(flags=flags, uncertain=uncertain))
    assert getattr(sample, field) is True
    # not_in_game / secure make the board untrustworthy; uncertain does NOT.
    assert sample.trustworthy is trustworthy


# -- drop + reconnect ---------------------------------------------------------


def test_eof_drops_link_and_next_poll_reconnects() -> None:
    dead = FakeLink([None])  # immediate EOF
    fresh = FakeLink([_packet(seq=9)])
    src, calls = _source(dead, fresh)
    assert src.poll() is None  # EOF -> drop
    assert dead.closed is True
    sample = src.poll()  # reconnects to the second link
    assert sample is not None and sample.seq == 9
    assert len(calls) == 2  # two connects: original + reconnect


def test_connect_failure_yields_none_and_retries_next_poll() -> None:
    # connector raises OSError (forward/APK down): poll -> None, no crash.
    src, _ = _source()  # empty queue -> connector raises OSError
    assert src.poll() is None
    assert src.poll() is None  # still safe on retry


def test_close_is_idempotent_and_closes_link() -> None:
    link = FakeLink([_packet()])
    src, _ = _source(link)
    src.poll()
    src.close()
    src.close()  # second close must not raise
    assert link.closed is True


# -- latest(): ping + drain to the newest -------------------------------------


def test_latest_pings_and_returns_newest_then_timeout() -> None:
    link = FakeLink([_packet(seq=1), _packet(seq=2), _packet(seq=3), _TIMEOUT])
    src, _ = _source(link)
    sample = src.latest()
    assert sample is not None
    assert sample.seq == 3  # drained to the freshest
    assert link.pings == 1  # forced a fresh sample


# -- forward setup ------------------------------------------------------------


def test_start_runs_forward_once_then_connects() -> None:
    forwards: list[int] = []

    def forwarder() -> bool:
        forwards.append(1)
        return True

    link = FakeLink([_packet()])
    src, calls = _source(link, forwarder=forwarder)
    src.start()
    assert forwards == [1]  # forward set up exactly once
    assert len(calls) == 1  # connected
    src.poll()
    assert forwards == [1]  # not forwarded again


def test_default_forwarder_builds_expected_adb_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    captured: dict[str, object] = {}

    class _Result:
        returncode = 0
        stderr = b""

    def fake_run(cmd: list[str], **kwargs: object) -> _Result:
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    src = ApkBoardSource(port=27184, serial="192.168.1.120:5555", adb_exe="adb")
    assert src._default_forwarder() is True
    assert captured["cmd"] == [
        "adb",
        "-s",
        "192.168.1.120:5555",
        "forward",
        "tcp:27184",
        "localabstract:bot2048_sampler",
    ]

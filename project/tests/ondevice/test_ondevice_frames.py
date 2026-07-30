"""Frame messages, new packet flags, host commands, NOT_IN_GAME detector."""

from __future__ import annotations

import struct

import pytest

from bot2048.ondevice.packet import (
    CMD_PING,
    CMD_SET_RATE,
    CMD_SYNC_REQ,
    FRAME_HEADER_SIZE,
    FRAME_MAGIC,
    FRAME_VERSION,
    MAX_FRAME_PAYLOAD,
    BoardPacket,
    FramePacket,
    FrameReason,
    PacketError,
    PacketFlag,
    crc16_ccitt,
    encode_set_rate,
)
from bot2048.ondevice.sampler import (
    NOT_IN_GAME_MIN_UNCERTAIN,
    NOT_IN_GAME_STREAK,
    NotInGameDetector,
)

# -- new packet flags --------------------------------------------------------


def test_new_flag_values_are_wire_stable() -> None:
    # The Kotlin side hard-codes these; a renumber would silently desync.
    assert PacketFlag.NOT_IN_GAME == 1 << 3
    assert PacketFlag.FRAME_FOLLOWS == 1 << 4


def test_new_flags_round_trip_in_board_packet() -> None:
    pkt = BoardPacket(
        board=(0,) * 16,
        seq=77,
        uncertain=0xFFFF,
        flags=PacketFlag.HAS_UNCERTAIN | PacketFlag.NOT_IN_GAME | PacketFlag.FRAME_FOLLOWS,
        device_ts_ms=1,
    )
    decoded = BoardPacket.decode(pkt.encode())
    assert PacketFlag.NOT_IN_GAME in decoded.flags
    assert PacketFlag.FRAME_FOLLOWS in decoded.flags


def test_legacy_golden_vector_unchanged_by_flag_extension() -> None:
    # The README golden vector for board_a.png must survive protocol growth.
    board = (0, 1, 2, 2, 0, 3, 1, 2, 0, 1, 3, 1, 0, 1, 3, 1)
    raw = BoardPacket(
        board=board, seq=1, uncertain=0, flags=PacketFlag.NONE, device_ts_ms=0
    ).encode()
    assert raw.hex() == "42320100000101220312013101310000000000007803"


# -- frame message -----------------------------------------------------------


def _frame(payload: bytes = b"\xff\xd8jpeg\xff\xd9", seq: int = 9) -> FramePacket:
    return FramePacket(reason=FrameReason.SYNC_REQ, seq=seq, payload=payload)


def test_frame_round_trip() -> None:
    frame = _frame()
    decoded = FramePacket.decode(frame.encode())
    assert decoded == frame
    assert decoded.reason is FrameReason.SYNC_REQ


def test_frame_header_layout() -> None:
    raw = _frame(payload=b"abc", seq=0x0102).encode()
    assert raw[:2] == FRAME_MAGIC
    assert raw[2] == FRAME_VERSION
    assert raw[3] == int(FrameReason.SYNC_REQ)
    assert raw[4:6] == b"\x01\x02"
    assert raw[6:10] == struct.pack(">I", 3)
    assert raw[10:12] == struct.pack(">H", crc16_ccitt(raw[:10]))
    assert raw[FRAME_HEADER_SIZE:] == b"abc"


def test_frame_header_rejects_bad_magic_version_crc() -> None:
    good = bytearray(_frame().encode()[:FRAME_HEADER_SIZE])
    bad_magic = bytes([ord("X")]) + bytes(good[1:])
    with pytest.raises(PacketError, match="magic"):
        FramePacket.decode_header(bad_magic)
    bad_version = bytearray(good)
    bad_version[2] = FRAME_VERSION + 1
    with pytest.raises(PacketError, match="version"):
        FramePacket.decode_header(bytes(bad_version))
    bad_crc = bytearray(good)
    bad_crc[4] ^= 0xFF
    with pytest.raises(PacketError, match="crc"):
        FramePacket.decode_header(bytes(bad_crc))


def test_frame_header_rejects_unknown_reason_and_absurd_length() -> None:
    head = struct.Struct(">2sBBHI").pack(FRAME_MAGIC, FRAME_VERSION, 250, 0, 4)
    raw = head + struct.pack(">H", crc16_ccitt(head))
    with pytest.raises(PacketError, match="reason"):
        FramePacket.decode_header(raw)
    head = struct.Struct(">2sBBHI").pack(
        FRAME_MAGIC, FRAME_VERSION, 0, 0, MAX_FRAME_PAYLOAD + 1
    )
    raw = head + struct.pack(">H", crc16_ccitt(head))
    with pytest.raises(PacketError, match="exceeds"):
        FramePacket.decode_header(raw)


def test_frame_decode_rejects_payload_length_mismatch() -> None:
    raw = _frame(payload=b"abcd").encode()
    with pytest.raises(PacketError, match="payload"):
        FramePacket.decode(raw[:-1])


def test_frame_rejects_out_of_range_seq() -> None:
    with pytest.raises(PacketError, match="seq"):
        FramePacket(reason=FrameReason.PERIODIC, seq=1 << 16, payload=b"")


# -- host commands -----------------------------------------------------------


def test_command_opcodes_are_wire_stable() -> None:
    assert (CMD_SYNC_REQ, CMD_PING, CMD_SET_RATE) == (0x53, 0x50, 0x52)


def test_encode_set_rate() -> None:
    assert encode_set_rate(0) == bytes((CMD_SET_RATE, 0))
    assert encode_set_rate(1000) == bytes((CMD_SET_RATE, 100))
    assert encode_set_rate(2550) == bytes((CMD_SET_RATE, 255))


@pytest.mark.parametrize("bad", [-10, 15, 2560])
def test_encode_set_rate_rejects_bad_interval(bad: int) -> None:
    with pytest.raises(PacketError, match="interval_ms"):
        encode_set_rate(bad)


# -- NOT_IN_GAME detector (spec for the Kotlin port) --------------------------


def _storm_mask() -> int:
    return (1 << NOT_IN_GAME_MIN_UNCERTAIN) - 1  # exactly the threshold popcount


def test_detector_latches_after_streak() -> None:
    detector = NotInGameDetector()
    states = [detector.update(_storm_mask()) for _ in range(NOT_IN_GAME_STREAK + 1)]
    assert states[: NOT_IN_GAME_STREAK - 1] == [False] * (NOT_IN_GAME_STREAK - 1)
    assert states[NOT_IN_GAME_STREAK - 1 :] == [True, True]


def test_detector_resets_on_confident_sample() -> None:
    detector = NotInGameDetector()
    for _ in range(NOT_IN_GAME_STREAK):
        detector.update(_storm_mask())
    assert detector.update(0) is False  # board again: streak broken
    assert detector.update(_storm_mask()) is False  # streak restarts from 1


def test_detector_counts_bits_not_value() -> None:
    detector = NotInGameDetector(min_uncertain=2, streak=1)
    assert detector.update(0b1000_0000_0000_0000) is False  # 1 bit, big value
    assert detector.update(0b11) is True


def test_detector_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        NotInGameDetector(min_uncertain=0)
    with pytest.raises(ValueError):
        NotInGameDetector(streak=0)

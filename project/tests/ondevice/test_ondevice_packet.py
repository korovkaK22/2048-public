"""BoardPacket wire format: round-trip, framing, CRC and validation."""

from __future__ import annotations

import struct

import pytest

from bot2048.ondevice.packet import (
    PACKET_MAGIC,
    PACKET_SIZE,
    PACKET_VERSION,
    BoardPacket,
    PacketError,
    PacketFlag,
    crc16_ccitt,
)


def _sample_board() -> tuple[int, ...]:
    return (0, 1, 2, 3, 0, 4, 5, 6, 0, 1, 2, 8, 0, 0, 15, 11)


def test_round_trip_preserves_all_fields() -> None:
    pkt = BoardPacket(
        board=_sample_board(),
        seq=4242,
        uncertain=0b1010_0000_0000_0101,
        flags=PacketFlag.BOARD_CHANGED | PacketFlag.HAS_UNCERTAIN,
        device_ts_ms=123_456_789,
    )
    decoded = BoardPacket.decode(pkt.encode())
    assert decoded == pkt


def test_encoded_length_and_header() -> None:
    raw = BoardPacket(
        board=(0,) * 16, seq=1, uncertain=0, flags=PacketFlag.NONE, device_ts_ms=0
    ).encode()
    assert len(raw) == PACKET_SIZE
    assert raw[:2] == PACKET_MAGIC
    assert raw[2] == PACKET_VERSION


def test_nibble_packing_high_low_order() -> None:
    # Cell 0 -> high nibble of byte 6, cell 1 -> low nibble of byte 6.
    board = tuple(range(16))  # exps 0..15 exercise the full nibble range
    raw = BoardPacket(
        board=board, seq=0, uncertain=0, flags=PacketFlag.NONE, device_ts_ms=0
    ).encode()
    board_bytes = raw[6:14]
    assert board_bytes[0] == (0 << 4) | 1
    assert board_bytes[7] == (14 << 4) | 15
    assert BoardPacket.decode(raw).board == board


def test_uncertain_cells_helper() -> None:
    pkt = BoardPacket(
        board=(0,) * 16,
        seq=0,
        uncertain=(1 << 3) | (1 << 15),
        flags=PacketFlag.NONE,
        device_ts_ms=0,
    )
    assert pkt.uncertain_cells() == (3, 15)


def test_flags_survive_as_intflag() -> None:
    pkt = BoardPacket(
        board=(0,) * 16, seq=0, uncertain=0, flags=PacketFlag.SECURE_SUSPECTED, device_ts_ms=7
    )
    decoded = BoardPacket.decode(pkt.encode())
    assert decoded.flags is PacketFlag.SECURE_SUSPECTED
    assert PacketFlag.SECURE_SUSPECTED in decoded.flags


@pytest.mark.parametrize("bad_len", [0, PACKET_SIZE - 1, PACKET_SIZE + 1])
def test_decode_rejects_wrong_length(bad_len: int) -> None:
    with pytest.raises(PacketError, match="must be 22 bytes"):
        BoardPacket.decode(b"\x00" * bad_len)


def test_decode_rejects_bad_magic() -> None:
    raw = bytearray(
        BoardPacket(
            board=(0,) * 16, seq=0, uncertain=0, flags=PacketFlag.NONE, device_ts_ms=0
        ).encode()
    )
    raw[0] = ord("X")
    # corrupting a covered byte also breaks the CRC; the magic check fires first
    with pytest.raises(PacketError, match="magic"):
        BoardPacket.decode(bytes(raw))


def test_decode_rejects_bad_version() -> None:
    head = struct.Struct(">2sBBH8sHI").pack(
        PACKET_MAGIC, PACKET_VERSION + 1, 0, 0, b"\x00" * 8, 0, 0
    )
    raw = head + struct.pack(">H", crc16_ccitt(head))
    with pytest.raises(PacketError, match="version"):
        BoardPacket.decode(raw)


def test_decode_rejects_crc_corruption() -> None:
    raw = bytearray(
        BoardPacket(
            board=_sample_board(), seq=9, uncertain=0, flags=PacketFlag.NONE, device_ts_ms=0
        ).encode()
    )
    raw[10] ^= 0xFF  # flip a board byte, leave the CRC field intact
    with pytest.raises(PacketError, match="crc mismatch"):
        BoardPacket.decode(bytes(raw))


def test_crc16_ccitt_known_vector() -> None:
    # CRC-16/CCITT-FALSE("123456789") == 0x29B1 (standard check value).
    assert crc16_ccitt(b"123456789") == 0x29B1


@pytest.mark.parametrize(
    ("field", "value"),
    [("seq", 1 << 16), ("uncertain", 1 << 16), ("device_ts_ms", 1 << 32), ("seq", -1)],
)
def test_construction_rejects_out_of_range(field: str, value: int) -> None:
    kwargs: dict[str, object] = {
        "board": (0,) * 16,
        "seq": 0,
        "uncertain": 0,
        "flags": PacketFlag.NONE,
        "device_ts_ms": 0,
    }
    kwargs[field] = value
    with pytest.raises(PacketError):
        BoardPacket(**kwargs)  # type: ignore[arg-type]


def test_construction_rejects_wrong_board_length() -> None:
    with pytest.raises(PacketError, match="16 cells"):
        BoardPacket(board=(0, 1, 2), seq=0, uncertain=0, flags=PacketFlag.NONE, device_ts_ms=0)


def test_encode_rejects_exponent_over_nibble() -> None:
    pkt = BoardPacket(
        board=(16,) + (0,) * 15, seq=0, uncertain=0, flags=PacketFlag.NONE, device_ts_ms=0
    )
    with pytest.raises(PacketError, match="nibble range"):
        pkt.encode()


def test_records_are_self_framing_after_resync() -> None:
    # Two concatenated records with a garbage prefix: a host that scans for the
    # magic and validates the CRC recovers the second clean record.
    good = BoardPacket(
        board=_sample_board(), seq=5, uncertain=0, flags=PacketFlag.NONE, device_ts_ms=1
    ).encode()
    stream = b"\x99\x99\x99" + good
    idx = stream.index(PACKET_MAGIC)
    assert BoardPacket.decode(stream[idx : idx + PACKET_SIZE]).seq == 5

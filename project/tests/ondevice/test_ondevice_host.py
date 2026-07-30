"""Host consumer: stream demux + SamplerLink over a mock socket."""

from __future__ import annotations

from collections import deque

from bot2048.ondevice.host import ProtocolDemuxer, SamplerLink
from bot2048.ondevice.packet import (
    CMD_PING,
    CMD_SET_RATE,
    CMD_SYNC_REQ,
    BoardPacket,
    FramePacket,
    FrameReason,
    PacketFlag,
)


def _packet(seq: int = 1, flags: PacketFlag = PacketFlag.NONE) -> BoardPacket:
    return BoardPacket(
        board=(0, 1, 2, 3, 0, 4, 5, 6, 0, 1, 2, 8, 0, 0, 15, 11),
        seq=seq,
        uncertain=0,
        flags=flags,
        device_ts_ms=42,
    )


def _frame(seq: int = 1, payload: bytes = b"\xff\xd8fake-jpeg\xff\xd9") -> FramePacket:
    return FramePacket(reason=FrameReason.PERIODIC, seq=seq, payload=payload)


class FakeSocket:
    """Deterministic SocketLike: recv() pops scripted chunks, sendall() records."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = deque(chunks)
        self.sent = bytearray()
        self.closed = False

    def recv(self, bufsize: int, /) -> bytes:
        del bufsize
        return self._chunks.popleft() if self._chunks else b""

    def sendall(self, data: bytes, /) -> None:
        self.sent.extend(data)

    def close(self) -> None:
        self.closed = True


# -- ProtocolDemuxer ----------------------------------------------------------


def test_demux_single_packet() -> None:
    demux = ProtocolDemuxer()
    assert demux.feed(_packet().encode()) == [_packet()]
    assert demux.discarded_bytes == 0


def test_demux_byte_at_a_time_packet_then_frame() -> None:
    demux = ProtocolDemuxer()
    stream = _packet(seq=7).encode() + _frame(seq=7).encode()
    got: list[object] = []
    for i in range(len(stream)):
        got.extend(demux.feed(stream[i : i + 1]))
    assert got == [_packet(seq=7), _frame(seq=7)]


def test_demux_resyncs_past_garbage_and_corruption() -> None:
    demux = ProtocolDemuxer()
    corrupt = bytearray(_packet(seq=1).encode())
    corrupt[10] ^= 0xFF  # break the CRC, keep the magic
    stream = b"\x99\x42\x99" + bytes(corrupt) + b"junk" + _packet(seq=2).encode()
    got = demux.feed(stream)
    assert [p.seq for p in got if isinstance(p, BoardPacket)] == [2]
    assert demux.discarded_bytes > 0


def test_demux_frame_payload_may_contain_packet_magic() -> None:
    # A JPEG byte-stream can contain b"B2"; the declared length must win.
    demux = ProtocolDemuxer()
    payload = b"xx" + _packet(seq=3).encode() + b"yy"
    frame = FramePacket(reason=FrameReason.INCIDENT, seq=3, payload=payload)
    got = demux.feed(frame.encode() + _packet(seq=4).encode())
    assert got == [frame, _packet(seq=4)]


def test_demux_skips_frame_header_with_absurd_length() -> None:
    demux = ProtocolDemuxer()
    bogus = bytearray(_frame().encode()[:12])
    bogus[6:10] = (0xFF, 0xFF, 0xFF, 0xFF)  # length 4 GiB; CRC now wrong too
    got = demux.feed(bytes(bogus) + _packet(seq=5).encode())
    assert got == [_packet(seq=5)]
    assert demux.discarded_bytes > 0


def test_demux_keeps_split_magic_across_feeds() -> None:
    demux = ProtocolDemuxer()
    raw = _packet(seq=6).encode()
    assert demux.feed(b"\x00\x00" + raw[:1]) == []  # buffer ends in b"B"
    assert demux.feed(raw[1:]) == [_packet(seq=6)]


# -- SamplerLink --------------------------------------------------------------


def test_link_reads_messages_until_eof() -> None:
    stream = _packet(seq=1).encode() + _frame(seq=1).encode() + _packet(seq=2).encode()
    sock = FakeSocket([stream[:10], stream[10:40], stream[40:]])
    link = SamplerLink(sock)
    assert link.read_message() == _packet(seq=1)
    assert link.read_message() == _frame(seq=1)
    assert link.read_message() == _packet(seq=2)
    assert link.read_message() is None  # EOF
    assert link.read_message() is None  # stays EOF


def test_link_commands_write_expected_bytes() -> None:
    sock = FakeSocket([])
    link = SamplerLink(sock)
    link.request_sync()
    link.ping()
    link.set_rate(500)
    assert bytes(sock.sent) == bytes((CMD_SYNC_REQ, CMD_PING, CMD_SET_RATE, 50))


def test_link_context_manager_closes_socket() -> None:
    sock = FakeSocket([])
    with SamplerLink(sock):
        pass
    assert sock.closed


def test_link_frame_follows_flag_pairs_packet_and_frame() -> None:
    # The device sets FRAME_FOLLOWS on the packet emitted just before a frame.
    pkt = _packet(seq=9, flags=PacketFlag.FRAME_FOLLOWS)
    sock = FakeSocket([pkt.encode() + _frame(seq=9).encode()])
    link = SamplerLink(sock)
    first = link.read_message()
    assert isinstance(first, BoardPacket)
    assert PacketFlag.FRAME_FOLLOWS in first.flags
    second = link.read_message()
    assert isinstance(second, FramePacket)
    assert second.seq == first.seq

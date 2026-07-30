"""The on-device -> host board packet: a fixed 22-byte, CRC-checked record.

The phone samples the 16 tile centers, classifies them against the palette and
emits ONE :class:`BoardPacket` per sampled frame on a persistent socket (an
abstract ``LocalServerSocket`` reached from the host via ``adb forward
localabstract:...``). The host reads fixed-size records and decodes them; the
whole board fits in ``tens of bytes``, so the ~442 KB PNG / 10 MB raw frame that
``screencap`` moves over WiFi never leaves the device on the fast path.

Wire format (big-endian, fixed :data:`PACKET_SIZE` = 22 bytes)::

    offset size field
    0      2    magic       b"B2"  (:data:`PACKET_MAGIC`; lets the host resync)
    2      1    version     :data:`PACKET_VERSION`
    3      1    flags       :class:`PacketFlag` bitfield
    4      2    seq         uint16, wraps -- sample counter (freshness / dedup)
    6      8    board       16 nibbles, row-major: byte j = cell 2j << 4 | 2j+1
    14     2    uncertain   uint16 bitmask, bit i set => cell i is low-confidence
    16     4    device_ts   uint32, device monotonic ms (low 32 bits) -- latency
    20     2    crc16       CRC-16/CCITT-FALSE over bytes[0:20]

Each cell exponent is 0..15 (0 = empty; 15 = 32768, far above 2048), so a nibble
per cell is exact. Fixed size + a leading magic + a trailing CRC means a host
that ever loses byte alignment can scan for the magic and validate with the CRC
rather than trusting a corrupt stream -- important for a long-running link.

The same socket also carries two low-rate message kinds (see
:class:`FramePacket` and the ``CMD_*`` constants):

* **device -> host frame message** (magic ``b"F2"``): a JPEG full frame,
  sent every ~10 s as the control tick, on a host ``SYNC_REQ``, or when the
  device detects an anomaly (incident). Variable-size, CRC-checked header::

      offset size field
      0      2    magic     b"F2" (:data:`FRAME_MAGIC`)
      2      1    version   :data:`FRAME_VERSION`
      3      1    reason    :class:`FrameReason`
      4      2    seq       uint16 -- seq of the board packet it accompanies
      6      4    length    uint32, JPEG payload bytes that follow the header
      10     2    crc16     CRC-16/CCITT-FALSE over bytes[0:10]
      12     ...  payload   JPEG bytes

  The board packet emitted immediately before a frame message carries
  :attr:`PacketFlag.FRAME_FOLLOWS`. A host that only wants boards can skip
  ``length`` bytes and keep reading packets.
* **host -> device commands**: single opcode bytes (:data:`CMD_SYNC_REQ`,
  :data:`CMD_PING`) or opcode + 1 argument byte (:data:`CMD_SET_RATE`, see
  :func:`encode_set_rate`).

Pure stdlib: no numpy, no phone. The identical packing is trivial to reproduce
in Kotlin/C on the device side (see docs/ondevice-vision-design.md and
``project/ondevice-apk``).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag

from bot2048.core.types import BOARD_CELLS

__all__ = [
    "CMD_PING",
    "CMD_SET_RATE",
    "CMD_SWIPE",
    "CMD_SYNC_REQ",
    "FRAME_HEADER_SIZE",
    "FRAME_MAGIC",
    "FRAME_VERSION",
    "MAX_FRAME_PAYLOAD",
    "PACKET_MAGIC",
    "PACKET_SIZE",
    "PACKET_VERSION",
    "SWIPE_ARG_SIZE",
    "BoardPacket",
    "FramePacket",
    "FrameReason",
    "PacketError",
    "PacketFlag",
    "crc16_ccitt",
    "encode_set_rate",
    "encode_swipe",
]

#: Two-byte record marker; also the resync token on a desynced stream.
PACKET_MAGIC: bytes = b"B2"

#: Wire-format version. Bump on any layout change (host rejects mismatches).
PACKET_VERSION: int = 1

#: Total fixed record length in bytes.
PACKET_SIZE: int = 22

#: 16-bit unsigned max (seq / uncertain-mask wrap point).
_U16_MAX: int = 0xFFFF

#: 32-bit unsigned max (device timestamp wrap point).
_U32_MAX: int = 0xFFFFFFFF

# struct for bytes[0:20] (everything the CRC covers): magic, version, flags,
# seq, 8 board bytes, uncertain, device_ts.
_HEAD = struct.Struct(">2sBBH8sHI")
assert _HEAD.size == PACKET_SIZE - 2


class PacketError(ValueError):
    """A byte buffer is not a valid :class:`BoardPacket` (magic/size/CRC/version)."""


class PacketFlag(IntFlag):
    """Bit flags carried in the packet ``flags`` byte."""

    NONE = 0
    #: The board differs from the previously emitted packet (a move/merge/spawn
    #: landed). The host uses this to confirm a swipe took effect.
    BOARD_CHANGED = 1 << 0
    #: At least one cell is low-confidence (nearest-palette distance over the
    #: sampler threshold): the host should reconcile with a full-frame digit
    #: read on its next control check. Mirrors ``uncertain`` being non-zero.
    HAS_UNCERTAIN = 1 << 1
    #: The sampled frame was (near) all-black: a suspected ``FLAG_SECURE``
    #: window (the monobank auth wall streams black, not pixels). The host
    #: falls back to the ``screencap`` 0-byte AUTH sensor -- board bytes on
    #: such a packet are meaningless.
    SECURE_SUSPECTED = 1 << 2
    #: The sampler no longer believes the game board is on screen: at least
    #: :data:`bot2048.ondevice.sampler.NOT_IN_GAME_MIN_UNCERTAIN` cells were
    #: uncertain for :data:`bot2048.ondevice.sampler.NOT_IN_GAME_STREAK`
    #: consecutive samples (popup / foreign screen / lockscreen). The device
    #: saves an incident screenshot and pushes an ``INCIDENT`` frame; board
    #: bytes on such a packet are unreliable.
    NOT_IN_GAME = 1 << 3
    #: A :class:`FramePacket` message follows this packet immediately on the
    #: same stream (periodic control tick, ``SYNC_REQ`` reply, or incident).
    FRAME_FOLLOWS = 1 << 4


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, xorout 0).

    Small, dependency-free and trivially reproducible on the device side. Used
    to validate each fixed-size record so a desynced or corrupted read is
    caught rather than decoded into a bogus board.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _pack_board(exponents: tuple[int, ...]) -> bytes:
    if len(exponents) != BOARD_CELLS:
        raise PacketError(f"expected {BOARD_CELLS} exponents, got {len(exponents)}")
    out = bytearray(BOARD_CELLS // 2)
    for i, exp in enumerate(exponents):
        if not 0 <= exp <= 0xF:
            raise PacketError(f"cell {i} exponent {exp} out of nibble range 0..15")
        if i % 2 == 0:
            out[i // 2] = exp << 4
        else:
            out[i // 2] |= exp
    return bytes(out)


def _unpack_board(raw: bytes) -> tuple[int, ...]:
    exps: list[int] = []
    for byte in raw:
        exps.append((byte >> 4) & 0xF)
        exps.append(byte & 0xF)
    return tuple(exps)


@dataclass(frozen=True, slots=True)
class BoardPacket:
    """One decoded on-device board sample.

    Attributes:
        board: 16 tile exponents, row-major (``index = row * 4 + col``), same
            layout as :data:`bot2048.core.types.Board`. ``0`` = empty cell.
        seq: device-side sample counter (wraps at 2**16); strictly increasing
            between wraps, so the host can drop stale/duplicate packets.
        uncertain: 16-bit mask; bit ``i`` set means cell ``i`` was classified
            with low confidence and should be re-checked with a full-frame
            digit read on the next control tick.
        flags: :class:`PacketFlag` bitfield (board-changed / has-uncertain /
            secure-suspected).
        device_ts_ms: device monotonic timestamp (low 32 bits, ms) when the
            frame was sampled -- for one-way latency / drift measurement.
    """

    board: tuple[int, ...]
    seq: int
    uncertain: int
    flags: PacketFlag
    device_ts_ms: int

    def __post_init__(self) -> None:
        if len(self.board) != BOARD_CELLS:
            raise PacketError(f"board must have {BOARD_CELLS} cells, got {len(self.board)}")
        if not 0 <= self.seq <= _U16_MAX:
            raise PacketError(f"seq {self.seq} out of uint16 range")
        if not 0 <= self.uncertain <= _U16_MAX:
            raise PacketError(f"uncertain mask {self.uncertain} out of uint16 range")
        if not 0 <= self.device_ts_ms <= _U32_MAX:
            raise PacketError(f"device_ts_ms {self.device_ts_ms} out of uint32 range")

    def uncertain_cells(self) -> tuple[int, ...]:
        """Indices of the cells flagged low-confidence (``uncertain`` bits set)."""
        return tuple(i for i in range(BOARD_CELLS) if self.uncertain & (1 << i))

    def encode(self) -> bytes:
        """Serialize to the fixed :data:`PACKET_SIZE`-byte wire record."""
        head = _HEAD.pack(
            PACKET_MAGIC,
            PACKET_VERSION,
            int(self.flags) & 0xFF,
            self.seq,
            _pack_board(self.board),
            self.uncertain,
            self.device_ts_ms,
        )
        return head + struct.pack(">H", crc16_ccitt(head))

    @classmethod
    def decode(cls, data: bytes) -> BoardPacket:
        """Parse one wire record. Raises :class:`PacketError` on any mismatch.

        Validates length, magic, version and CRC before trusting a single byte
        of the board, so a corrupt or misaligned buffer never becomes a board.
        """
        if len(data) != PACKET_SIZE:
            raise PacketError(f"packet must be {PACKET_SIZE} bytes, got {len(data)}")
        head, crc_bytes = data[: _HEAD.size], data[_HEAD.size :]
        (magic, version, flags, seq, board_raw, uncertain, device_ts) = _HEAD.unpack(head)
        if magic != PACKET_MAGIC:
            raise PacketError(f"bad magic {magic!r}, expected {PACKET_MAGIC!r}")
        if version != PACKET_VERSION:
            raise PacketError(f"unsupported packet version {version} (expected {PACKET_VERSION})")
        (crc,) = struct.unpack(">H", crc_bytes)
        expected = crc16_ccitt(head)
        if crc != expected:
            raise PacketError(f"crc mismatch: got 0x{crc:04x}, computed 0x{expected:04x}")
        return cls(
            board=_unpack_board(board_raw),
            seq=seq,
            uncertain=uncertain,
            flags=PacketFlag(flags),
            device_ts_ms=device_ts,
        )


# ---------------------------------------------------------------------------
# Frame message (device -> host): JPEG control frame on the same stream.
# ---------------------------------------------------------------------------

#: Two-byte frame-message marker (board packets use :data:`PACKET_MAGIC`).
FRAME_MAGIC: bytes = b"F2"

#: Frame-message header version. Bump on any header layout change.
FRAME_VERSION: int = 1

#: Fixed frame-message header length (the JPEG payload follows it).
FRAME_HEADER_SIZE: int = 12

#: Sanity cap on the declared JPEG payload length. A real 1080x2400 JPEG at
#: quality 80 is ~200-500 KB; anything past this cap is a corrupt/desynced
#: header, not a frame, and the demuxer resyncs instead of buffering it.
MAX_FRAME_PAYLOAD: int = 8 * 1024 * 1024

# struct for header bytes[0:10]: magic, version, reason, seq, payload length.
_FRAME_HEAD = struct.Struct(">2sBBHI")
assert _FRAME_HEAD.size == FRAME_HEADER_SIZE - 2


class FrameReason(IntEnum):
    """Why the device pushed a full frame (byte 3 of the frame header)."""

    #: The ~10 s control tick: digit reconciliation, popup/score OCR, drift.
    PERIODIC = 0
    #: Direct reply to a host :data:`CMD_SYNC_REQ`.
    SYNC_REQ = 1
    #: The device detected an anomaly (``NOT_IN_GAME`` uncertain-storm); the
    #: same JPEG is also saved on-device for later research.
    INCIDENT = 2


@dataclass(frozen=True, slots=True)
class FramePacket:
    """One device -> host JPEG frame message (header + payload).

    Attributes:
        reason: why the frame was pushed (:class:`FrameReason`).
        seq: ``seq`` of the board packet this frame accompanies (the packet
            that carried :attr:`PacketFlag.FRAME_FOLLOWS`).
        payload: the JPEG bytes.
    """

    reason: FrameReason
    seq: int
    payload: bytes

    def __post_init__(self) -> None:
        if not 0 <= self.seq <= _U16_MAX:
            raise PacketError(f"seq {self.seq} out of uint16 range")
        if len(self.payload) > MAX_FRAME_PAYLOAD:
            raise PacketError(f"frame payload {len(self.payload)} exceeds {MAX_FRAME_PAYLOAD}")

    def encode(self) -> bytes:
        """Serialize to header (:data:`FRAME_HEADER_SIZE` bytes) + payload."""
        head = _FRAME_HEAD.pack(
            FRAME_MAGIC, FRAME_VERSION, int(self.reason), self.seq, len(self.payload)
        )
        return head + struct.pack(">H", crc16_ccitt(head)) + self.payload

    @staticmethod
    def decode_header(data: bytes) -> tuple[FrameReason, int, int]:
        """Validate a :data:`FRAME_HEADER_SIZE`-byte header; return its fields.

        Returns ``(reason, seq, payload_length)`` -- the caller then reads
        ``payload_length`` bytes of JPEG. Raises :class:`PacketError` on bad
        length/magic/version/CRC/reason or an implausible payload length.
        """
        if len(data) != FRAME_HEADER_SIZE:
            raise PacketError(
                f"frame header must be {FRAME_HEADER_SIZE} bytes, got {len(data)}"
            )
        head, crc_bytes = data[: _FRAME_HEAD.size], data[_FRAME_HEAD.size :]
        (magic, version, reason_raw, seq, length) = _FRAME_HEAD.unpack(head)
        if magic != FRAME_MAGIC:
            raise PacketError(f"bad frame magic {magic!r}, expected {FRAME_MAGIC!r}")
        if version != FRAME_VERSION:
            raise PacketError(f"unsupported frame version {version} (expected {FRAME_VERSION})")
        (crc,) = struct.unpack(">H", crc_bytes)
        expected = crc16_ccitt(head)
        if crc != expected:
            raise PacketError(f"frame crc mismatch: got 0x{crc:04x}, computed 0x{expected:04x}")
        if length > MAX_FRAME_PAYLOAD:
            raise PacketError(f"frame payload length {length} exceeds {MAX_FRAME_PAYLOAD}")
        try:
            reason = FrameReason(reason_raw)
        except ValueError as exc:
            raise PacketError(f"unknown frame reason {reason_raw}") from exc
        return reason, seq, length

    @classmethod
    def decode(cls, data: bytes) -> FramePacket:
        """Parse one complete frame message (header + full payload)."""
        if len(data) < FRAME_HEADER_SIZE:
            raise PacketError(
                f"frame message needs >= {FRAME_HEADER_SIZE} bytes, got {len(data)}"
            )
        reason, seq, length = cls.decode_header(data[:FRAME_HEADER_SIZE])
        payload = data[FRAME_HEADER_SIZE:]
        if len(payload) != length:
            raise PacketError(f"frame payload is {len(payload)} bytes, header says {length}")
        return cls(reason=reason, seq=seq, payload=payload)


# ---------------------------------------------------------------------------
# Host -> device commands: single opcode bytes on the same socket.
# ---------------------------------------------------------------------------

#: ``b"S"`` -- push one full JPEG frame now (:attr:`FrameReason.SYNC_REQ`).
CMD_SYNC_REQ: int = 0x53

#: ``b"P"`` -- emit a board packet immediately even if the board is unchanged
#: (liveness probe; the reply is a normal :class:`BoardPacket`).
CMD_PING: int = 0x50

#: ``b"R"`` + 1 argument byte -- set the unchanged-board heartbeat interval to
#: ``arg * 10`` ms (0 = emit every captured frame). See :func:`encode_set_rate`.
CMD_SET_RATE: int = 0x52

#: ``b"W"`` + :data:`SWIPE_ARG_SIZE` argument bytes -- inject a swipe gesture ON
#: THE DEVICE via the sampler's :class:`AccessibilityService`
#: (``dispatchGesture``), instead of the host spawning ``adb shell input
#: swipe``. This removes both the per-move Dalvik ``input`` process spawn AND
#: the adb-shell hop (~60 ms/move on this host). See :func:`encode_swipe`; the
#: device port lives in ``ondevice-apk`` (SwipeAccessibilityService +
#: SamplerService.readCommands). Fire-and-forget: the device dispatches the
#: gesture; the host decides availability with a one-time capability probe (is
#: the accessibility service enabled?) and falls back to ``input swipe`` when
#: it is not -- a move is never dropped.
CMD_SWIPE: int = 0x57

#: Argument byte count following :data:`CMD_SWIPE`: five big-endian uint16
#: fields ``(x1, y1, x2, y2, duration_ms)``.
SWIPE_ARG_SIZE: int = 10

#: uint16 upper bound for the swipe fields (coords + duration).
_U16_MAX_SWIPE: int = _U16_MAX

# struct for the CMD_SWIPE payload (opcode byte + 5 uint16 fields).
_SWIPE = struct.Struct(">B5H")
assert _SWIPE.size == 1 + SWIPE_ARG_SIZE


def encode_set_rate(interval_ms: int) -> bytes:
    """Encode a ``SET_RATE`` command for a heartbeat of ``interval_ms`` ms.

    The wire argument is one byte in units of 10 ms, so ``interval_ms`` must
    be a multiple of 10 in ``0..2550``; ``0`` makes the device emit a packet
    for every captured frame (changed or not).
    """
    if not 0 <= interval_ms <= 2550 or interval_ms % 10:
        raise PacketError(
            f"interval_ms must be a multiple of 10 in 0..2550, got {interval_ms}"
        )
    return bytes((CMD_SET_RATE, interval_ms // 10))


def encode_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> bytes:
    """Encode a :data:`CMD_SWIPE` command: opcode + 5 big-endian uint16 fields.

    Wire layout (11 bytes total): ``b"W"`` then ``x1 y1 x2 y2 duration_ms``,
    each a uint16 big-endian. Endpoints are absolute display pixels (the same
    coordinates :class:`bot2048.device.humanizer.SwipeParams` already carries,
    already safe-zone clamped); ``duration_ms`` is the gesture stroke duration.

    All five fields must fit an unsigned 16-bit int (0..65535); ``duration_ms``
    must be >= 1 (a zero-length gesture is rejected by the device). The device
    reconstructs the identical ``(x1, y1, x2, y2, duration_ms)`` and feeds it to
    ``GestureDescription.StrokeDescription`` on its accessibility service.
    """
    fields = (x1, y1, x2, y2, duration_ms)
    for name, value in zip(("x1", "y1", "x2", "y2", "duration_ms"), fields, strict=True):
        if not 0 <= value <= _U16_MAX_SWIPE:
            raise PacketError(f"{name}={value} out of uint16 range 0..{_U16_MAX_SWIPE}")
    if duration_ms < 1:
        raise PacketError(f"duration_ms must be >= 1, got {duration_ms}")
    return _SWIPE.pack(CMD_SWIPE, x1, y1, x2, y2, duration_ms)

"""On-device vision offload: compact board-state packets + reference sampler.

The operator's idea (docs/ondevice-vision-design.md): instead of shipping full
1080x2400 frames to the host every move and doing all vision host-side, do the
*cheap color math on the phone* -- sample the 16 tile-center colors, classify
each against the calibrated palette, and push a tiny fixed-size **board packet**
(:mod:`bot2048.ondevice.packet`, 22 bytes) to the host over a persistent socket
(``adb forward``). A full-frame screenshot is pulled only every ~10 s as a
control check (digit OCR, score/timer, popup/auth, palette drift).

This package is the **host-side reference + wire contract**, fully offline and
unit-tested:

* :mod:`bot2048.ondevice.packet` -- the ``BoardPacket`` binary format
  (encode/decode, CRC-checked, resyncable) shared by device and host.
* :mod:`bot2048.ondevice.sampler` -- the pure Python reference implementation
  of the on-device sampling algorithm, reusing the exact color path of
  :mod:`bot2048.vision.parser` so the two never diverge. The on-device
  Kotlin/NDK sampler is a port of this function; the golden packet vectors in
  ``tests/ondevice`` are its conformance contract.
* :mod:`bot2048.ondevice.host` -- the host-side consumer: a resyncing stream
  demuxer (board packets + JPEG control frames on one socket) and the
  ``SYNC_REQ`` / ``PING`` / ``SET_RATE`` command writers, socket-agnostic and
  mock-testable.

Nothing here talks to a phone or imports adb -- it runs on recorded frames
(:func:`bot2048.ondevice.host.connect_sampler` is the one opt-in exception).
"""

from bot2048.ondevice.host import (
    DEFAULT_FORWARD_PORT,
    ProtocolDemuxer,
    SamplerLink,
    connect_sampler,
)
from bot2048.ondevice.packet import (
    PACKET_MAGIC,
    PACKET_SIZE,
    PACKET_VERSION,
    BoardPacket,
    FramePacket,
    FrameReason,
    PacketError,
    PacketFlag,
    crc16_ccitt,
)
from bot2048.ondevice.sampler import (
    NOT_IN_GAME_MIN_UNCERTAIN,
    NOT_IN_GAME_STREAK,
    UNCERTAIN_MAX_DIST,
    NotInGameDetector,
    build_packet,
    is_black_frame,
    sample_board_reference,
    sample_cell_exponents,
)

__all__ = [
    "DEFAULT_FORWARD_PORT",
    "NOT_IN_GAME_MIN_UNCERTAIN",
    "NOT_IN_GAME_STREAK",
    "PACKET_MAGIC",
    "PACKET_SIZE",
    "PACKET_VERSION",
    "UNCERTAIN_MAX_DIST",
    "BoardPacket",
    "FramePacket",
    "FrameReason",
    "NotInGameDetector",
    "PacketError",
    "PacketFlag",
    "ProtocolDemuxer",
    "SamplerLink",
    "build_packet",
    "connect_sampler",
    "crc16_ccitt",
    "is_black_frame",
    "sample_board_reference",
    "sample_cell_exponents",
]

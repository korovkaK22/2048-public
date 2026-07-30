"""Host-side consumer of the on-device sampler stream.

The APK publishes an abstract ``LocalServerSocket`` (``bot2048_sampler``); the
host reaches it with::

    adb forward tcp:27184 localabstract:bot2048_sampler

and connects a TCP client to ``127.0.0.1:27184``. The stream carries two
device -> host message kinds, demuxed by their 2-byte magic
(:mod:`bot2048.ondevice.packet` is the wire contract):

* ``b"B2"`` -- fixed 22-byte :class:`~bot2048.ondevice.packet.BoardPacket`;
* ``b"F2"`` -- variable-size :class:`~bot2048.ondevice.packet.FramePacket`
  (12-byte CRC-checked header + JPEG payload).

Host -> device commands (``SYNC_REQ`` / ``PING`` / ``SET_RATE``) go over the
same socket as opcode bytes.

Two layers, both fully testable without a phone:

* :class:`ProtocolDemuxer` -- pure bytes-in / messages-out incremental parser
  with magic-scan resync (a corrupt or half-read stream never becomes a bogus
  board -- CRC decides).
* :class:`SamplerLink` -- the blocking reader/writer around any socket-like
  object (``recv``/``sendall``/``close``), plus :func:`connect_sampler` for
  the real TCP case.

This module deliberately does NOT implement a ``FrameSource`` or touch the
runner/capture seams -- wiring the packet stream into the live loop belongs to
the runner-owning agent (docs/ondevice-vision-design.md §5).
"""

from __future__ import annotations

import contextlib
from types import TracebackType
from typing import Protocol, Self

import structlog

from bot2048.ondevice.packet import (
    CMD_PING,
    CMD_SYNC_REQ,
    FRAME_HEADER_SIZE,
    FRAME_MAGIC,
    PACKET_MAGIC,
    PACKET_SIZE,
    BoardPacket,
    FramePacket,
    PacketError,
    encode_set_rate,
    encode_swipe,
)

__all__ = [
    "DEFAULT_FORWARD_PORT",
    "Message",
    "ProtocolDemuxer",
    "SamplerLink",
    "SocketLike",
    "connect_sampler",
]

_log = structlog.get_logger(__name__)

#: Host TCP port conventionally forwarded to ``localabstract:bot2048_sampler``.
DEFAULT_FORWARD_PORT: int = 27184

#: Everything the stream can yield.
Message = BoardPacket | FramePacket


class SocketLike(Protocol):
    """The minimal socket surface :class:`SamplerLink` needs (mock-friendly)."""

    def recv(self, bufsize: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...

    def close(self) -> None: ...


class ProtocolDemuxer:
    """Incremental, resyncing parser: raw stream chunks -> decoded messages.

    Feed arbitrary chunks (any split points) into :meth:`feed`; it returns
    every complete message and buffers the rest. Garbage between messages --
    a desynced read, a corrupted record -- is skipped by scanning for the next
    magic and letting the CRC arbitrate; :attr:`discarded_bytes` counts what
    was dropped (a nonzero value on a healthy link means trouble worth
    logging, not crashing).
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        #: Total garbage bytes skipped while resyncing (diagnostic).
        self.discarded_bytes: int = 0

    def feed(self, chunk: bytes) -> list[Message]:
        """Consume one stream chunk; return all messages completed by it."""
        self._buf.extend(chunk)
        out: list[Message] = []
        while True:
            message = self._next_message()
            if message is None:
                return out
            out.append(message)

    def _discard(self, count: int) -> None:
        del self._buf[:count]
        self.discarded_bytes += count

    def _next_message(self) -> Message | None:
        """Parse one message off the buffer, resyncing past garbage; None = need more."""
        while True:
            start = self._scan_to_magic()
            if start is None:
                return None  # no full magic in the buffer yet
            if self._buf[start : start + 2] == PACKET_MAGIC:
                if len(self._buf) - start < PACKET_SIZE:
                    return None
                try:
                    packet = BoardPacket.decode(bytes(self._buf[start : start + PACKET_SIZE]))
                except PacketError:
                    self._discard(start + 2)  # false magic: skip it, rescan
                    continue
                del self._buf[: start + PACKET_SIZE]
                return packet
            # FRAME_MAGIC
            if len(self._buf) - start < FRAME_HEADER_SIZE:
                return None
            header = bytes(self._buf[start : start + FRAME_HEADER_SIZE])
            try:
                reason, seq, length = FramePacket.decode_header(header)
            except PacketError:
                self._discard(start + 2)
                continue
            total = FRAME_HEADER_SIZE + length
            if len(self._buf) - start < total:
                return None
            payload = bytes(self._buf[start + FRAME_HEADER_SIZE : start + total])
            del self._buf[: start + total]
            return FramePacket(reason=reason, seq=seq, payload=payload)

    def _scan_to_magic(self) -> int | None:
        """Index of the earliest magic; drops bytes that can never start one."""
        i_packet = self._buf.find(PACKET_MAGIC)
        i_frame = self._buf.find(FRAME_MAGIC)
        candidates = [i for i in (i_packet, i_frame) if i >= 0]
        if not candidates:
            # Keep the last byte: it may be the first half of a split magic.
            if len(self._buf) > 1:
                self._discard(len(self._buf) - 1)
            return None
        start = min(candidates)
        if start:
            self._discard(start)
        return 0


class SamplerLink:
    """Blocking reader/writer for one sampler connection (socket-agnostic).

    Wraps any :class:`SocketLike` -- a real TCP socket to the ``adb forward``
    port, or one end of a ``socket.socketpair()`` in tests -- and exposes the
    stream as decoded messages plus the three host commands.
    """

    def __init__(self, sock: SocketLike, *, recv_size: int = 65536) -> None:
        if recv_size < 1:
            raise ValueError(f"recv_size must be >= 1, got {recv_size}")
        self._sock = sock
        self._recv_size = recv_size
        self._demux = ProtocolDemuxer()
        self._ready: list[Message] = []
        self._eof = False

    def read_message(self) -> Message | None:
        """Next decoded message, blocking on the socket; ``None`` on EOF."""
        while not self._ready:
            if self._eof:
                return None
            chunk = self._sock.recv(self._recv_size)
            if not chunk:
                self._eof = True
                if self._demux.discarded_bytes:
                    _log.warning(
                        "ondevice.host.stream_garbage",
                        discarded_bytes=self._demux.discarded_bytes,
                    )
                return None
            self._ready.extend(self._demux.feed(chunk))
        return self._ready.pop(0)

    def request_sync(self) -> None:
        """Ask the device for one full JPEG frame (``FrameReason.SYNC_REQ``)."""
        self._sock.sendall(bytes((CMD_SYNC_REQ,)))

    def ping(self) -> None:
        """Ask for an immediate board packet even if the board is unchanged."""
        self._sock.sendall(bytes((CMD_PING,)))

    def set_rate(self, interval_ms: int) -> None:
        """Set the device's unchanged-board heartbeat (see :func:`encode_set_rate`)."""
        self._sock.sendall(encode_set_rate(interval_ms))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        """Ask the device to inject a swipe gesture (see :func:`encode_swipe`).

        Writes a :data:`~bot2048.ondevice.packet.CMD_SWIPE` command over the
        same socket that carries board packets; the device's accessibility
        service dispatches the gesture. Fire-and-forget (no ack on the read
        stream): the caller times the gesture itself and decides fallback via a
        one-time capability probe. ``sendall`` on the socket is safe alongside
        the blocking ``recv`` the drain thread runs.
        """
        self._sock.sendall(encode_swipe(x1, y1, x2, y2, duration_ms))

    def close(self) -> None:
        """Close the underlying socket. Safe to call more than once."""
        # platform-dependent double-close
        with contextlib.suppress(OSError):
            self._sock.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def connect_sampler(
    port: int = DEFAULT_FORWARD_PORT,
    host: str = "127.0.0.1",
    *,
    timeout_s: float | None = 5.0,
) -> SamplerLink:
    """Connect to the forwarded sampler socket and return a :class:`SamplerLink`.

    Prerequisite (once per adb session)::

        adb forward tcp:27184 localabstract:bot2048_sampler

    ``timeout_s`` applies to connect *and* subsequent reads (``None`` blocks
    forever); a read timeout raises ``TimeoutError`` from ``recv`` -- callers
    treat that as "no packet yet", not a dead link.
    """
    import socket

    sock = socket.create_connection((host, port), timeout=timeout_s)
    return SamplerLink(sock)

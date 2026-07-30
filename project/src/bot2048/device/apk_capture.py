"""On-device APK board source: 22-byte packets as the live loop's fast board.

The MediaProjection helper APK (``ai.bot2048.sampler``, option **D** in
docs/ondevice-vision-design.md) samples the 16 tile centers ON THE PHONE and
pushes a fixed :class:`~bot2048.ondevice.packet.BoardPacket` over a persistent
``adb forward`` socket. The board arrives ALREADY recognized (exponents), so
the live loop can skip the scrcpy-video -> ``vision.parse_board`` chain for the
per-move board and read it in ~one socket round-trip (~50 ms, measured ~20x
faster than a scrcpy frame decode+sample).

This module is the host consumer the runner wires in (the seam
docs/ondevice-vision-design.md §5 left to the runner-owning agent). It wraps
:func:`bot2048.ondevice.host.connect_sampler` (the transport contract, read
only) and adds exactly what the live loop needs on top of the raw link:

* :class:`BoardSample` -- one decoded board plus the decision-relevant packet
  flags (changed / not-in-game / secure-suspected / uncertain), with a
  :attr:`~BoardSample.trustworthy` gate the loop uses to decide "read the
  packet" vs "fail-closed to a full frame", and :attr:`~BoardSample.age_ms` --
  how long the packet sat in the host mailbox before the loop consumed it.
* :class:`ApkBoardSource` -- lazily sets up the ``adb forward``, connects, and
  serves the freshest board on request; a dropped link is transparently
  reconnected, and a link that stays silent/dead simply yields ``None`` so the
  caller falls back to the ``screencap`` full-frame path (never a blind tap).

Concurrency model (the background stream drain): the device is a PUSH server --
it emits a packet the instant the board changes -- so a background daemon
thread (one per live link) drains the socket continuously and publishes every
decoded :class:`BoardPacket` into a 1-slot mailbox (newest wins, stamped with
the host-monotonic receive time). The consumer-facing :meth:`ApkBoardSource.poll`
never touches the socket: it hands over the freshest UNCONSUMED mailbox packet
immediately (freshness tracked by a wrap-safe publish generation counter, so a
repeated ``seq`` is never re-delivered), or waits up to ``read_timeout_s`` for
the next publish -- exactly the blocking budget the old single-threaded socket
read had, but a packet the phone already pushed no longer sits in the socket
buffer until the loop's next poll. Everything else is unchanged: a silent or
dropped link still degrades to ``None`` and the runner's fail-closed fallback
ladder (packet -> predict-gate frames -> stable-frame settle) is untouched.

What this module deliberately does NOT do: state detection. The 22-byte packet
cannot see popups / the CONFIRM dialog / the ``FLAG_SECURE`` auth wall reliably
(a black auth frame only *suspects* secure), so ``detect_state`` stays on
periodic full frames in the runner -- this source only ever answers "what is
the board right now, and can I trust it?".
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

import structlog

from bot2048.core.types import Board
from bot2048.ondevice.host import DEFAULT_FORWARD_PORT, Message, connect_sampler
from bot2048.ondevice.packet import BoardPacket, PacketFlag

__all__ = [
    "DEFAULT_SAMPLER_SOCKET",
    "ApkBoardSource",
    "BoardSample",
    "LinkLike",
]

log = structlog.get_logger(__name__)

#: The abstract ``LocalServerSocket`` the APK publishes (see the APK manifest /
#: docs/ondevice-vision-design.md §5). ``adb forward tcp:<port>
#: localabstract:<this>`` bridges it to a host TCP port.
DEFAULT_SAMPLER_SOCKET: str = "bot2048_sampler"

#: :meth:`ApkBoardSource.latest` quiescence window (s): after the ping reply
#: lands, keep consuming as long as newer publishes keep arriving within this
#: window, so a burst settles to its newest packet (the old drain-to-newest
#: semantics) without latest() ever busy-reading the socket itself.
_LATEST_QUIESCE_S: float = 0.02


class LinkLike(Protocol):
    """The slice of :class:`bot2048.ondevice.host.SamplerLink` this source uses.

    A real link (:func:`~bot2048.ondevice.host.connect_sampler`) satisfies it;
    tests inject a fake with scripted ``read_message`` behavior (including a
    ``TimeoutError`` for "no packet yet" and ``None`` for EOF).

    ``read_message`` is called from the background drain thread only;
    ``ping``/``swipe``/``close`` are called from the consumer thread (a
    ``sendall`` / ``close`` on a socket is safe alongside a blocking ``recv``).
    """

    def read_message(self) -> Message | None: ...

    def ping(self) -> None: ...

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None: ...

    def close(self) -> None: ...


#: ``(port, host, timeout_s) -> LinkLike`` -- how a new link is opened.
Connector = Callable[[int, str, float], LinkLike]

#: ``() -> bool`` -- best-effort ``adb forward`` setup; ``True`` on success.
Forwarder = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class BoardSample:
    """One on-device board sample: the board plus the flags the loop acts on.

    Attributes:
        board: 16 tile exponents, row-major -- the same :data:`Board` layout
            the engine and vision use (0 = empty), read directly off the
            packet, no host-side vision needed.
        seq: device sample counter (wraps at 2**16); lets the caller tell a
            fresh sample from a repeat.
        changed: the board differs from the previously emitted packet
            (``FLAG_BOARD_CHANGED``) -- a move/merge/spawn landed.
        not_in_game: the sampler no longer believes the board is on screen
            (``FLAG_NOT_IN_GAME``: popup / foreign screen). Board bytes are
            unreliable.
        secure_suspected: the frame was (near) all-black
            (``FLAG_SECURE_SUSPECTED``): a suspected auth wall. Board bytes are
            meaningless.
        uncertain: at least one cell was low-confidence
            (``FLAG_HAS_UNCERTAIN`` or a non-zero uncertain mask): the host
            should reconcile with a full-frame digit read on its next tick.
        age_ms: host-side packet age -- milliseconds between the drain thread
            receiving/decoding the packet and the consumer being handed this
            sample (mailbox dwell time). ``None`` when the producer cannot
            measure it (bare test doubles). Telemetry only: measures exactly
            the latency the background drain exists to remove.
    """

    board: Board
    seq: int
    changed: bool
    not_in_game: bool
    secure_suspected: bool
    uncertain: bool
    age_ms: float | None = None

    @classmethod
    def from_packet(
        cls, packet: BoardPacket, *, age_ms: float | None = None
    ) -> BoardSample:
        """Project a decoded :class:`BoardPacket` onto the loop's decision flags."""
        flags = packet.flags
        return cls(
            board=packet.board,
            seq=packet.seq,
            changed=bool(flags & PacketFlag.BOARD_CHANGED),
            not_in_game=bool(flags & PacketFlag.NOT_IN_GAME),
            secure_suspected=bool(flags & PacketFlag.SECURE_SUSPECTED),
            uncertain=bool(flags & PacketFlag.HAS_UNCERTAIN) or packet.uncertain != 0,
            age_ms=age_ms,
        )

    @property
    def trustworthy(self) -> bool:
        """The board bytes are meaningful (in-game, not a black/secure frame).

        ``uncertain`` is deliberately NOT disqualifying here: an uncertain cell
        still carries a best-guess exponent that the runner's predict-gate can
        confirm against the deterministic engine prediction. Only
        ``not_in_game`` / ``secure_suspected`` -- where the packet itself says
        the board is not on screen -- make the bytes untrustworthy, and the
        runner then confirms with a full frame (fail-closed).
        """
        return not (self.not_in_game or self.secure_suspected)


def _default_connector(port: int, host: str, timeout_s: float) -> LinkLike:
    """Open a real forwarded sampler link (the transport contract, read only).

    ``timeout_s`` doubles as connect and per-read timeout; the socket is the
    local ``adb forward`` port (127.0.0.1), so connect is effectively instant
    and a short timeout only bounds how long a silent read blocks.
    """
    return connect_sampler(port, host, timeout_s=timeout_s)


class ApkBoardSource:
    """Serves the freshest on-device board on request; reconnects transparently.

    Lifecycle mirrors a :class:`~bot2048.device.capture.FrameSource`:
    ``start()`` is an eager, best-effort warm-up (forward + connect + drain
    start) that never raises; :meth:`poll` / :meth:`latest` lazily (re)connect
    on demand, so a link that drops mid-run is rebuilt on the next call. Every
    read failure degrades to ``None`` -- "no board from the fast path" -- which
    the runner treats as "fall back to the ``screencap`` full frame", never as
    a board.

    Concurrency (see the module docstring): one background daemon thread per
    live link drains the socket into a 1-slot mailbox under ``self._cond``;
    the consumer thread (the live loop) only ever reads the mailbox. The drain
    thread is keyed to its link object -- when the link is dropped or replaced
    the stale thread notices (identity check) and exits, so a reconnect can
    never end up with two readers on one socket. ``ping``/``set_rate`` style
    writes and ``close`` happen on the consumer thread; writing/closing a
    socket concurrently with a blocking ``recv`` is safe.
    """

    def __init__(
        self,
        *,
        port: int = DEFAULT_FORWARD_PORT,
        host: str = "127.0.0.1",
        serial: str | None = None,
        adb_exe: str = "adb",
        socket_name: str = DEFAULT_SAMPLER_SOCKET,
        read_timeout_s: float = 0.15,
        connector: Connector = _default_connector,
        forwarder: Forwarder | None = None,
    ) -> None:
        if not (0 < port < 65536):
            raise ValueError(f"invalid port {port}")
        if read_timeout_s <= 0:
            raise ValueError(f"read_timeout_s must be > 0, got {read_timeout_s}")
        self._port = port
        self._host = host
        self._serial = serial
        self._adb_exe = adb_exe
        self._socket_name = socket_name
        self._read_timeout_s = read_timeout_s
        self._connector = connector
        self._forwarder = forwarder or self._default_forwarder
        self._forwarded = False
        # -- shared state (mailbox + link), guarded by self._cond ------------
        self._cond = threading.Condition()
        self._link: LinkLike | None = None
        #: 1-slot mailbox: the newest decoded packet + host-monotonic recv time.
        self._slot: tuple[BoardPacket, float] | None = None
        #: Publish generation: +1 per packet the drain publishes. Freshness is
        #: "generation advanced", never raw ``seq`` arithmetic, so the u16 seq
        #: wrap can not make a new packet look stale.
        self._gen = 0
        #: Generation last handed out by :meth:`poll`/:meth:`latest` -- a
        #: packet is delivered at most once (a repeat poll of a quiet board is
        #: ``None``, exactly like the old blocking read's timeout).
        self._consumed = 0

    # -- adb forward --------------------------------------------------------

    def _default_forwarder(self) -> bool:
        """``adb [-s serial] forward tcp:<port> localabstract:<socket>``.

        Spawned directly (no shell) so git-bash path mangling never applies;
        re-running an identical forward is harmless. Returns ``False`` (logged)
        on any failure -- the caller still tries to connect, since the forward
        may already be up from a previous adb session.
        """
        cmd = [self._adb_exe]
        if self._serial is not None:
            cmd += ["-s", self._serial]
        cmd += ["forward", f"tcp:{self._port}", f"localabstract:{self._socket_name}"]
        try:
            result = subprocess.run(cmd, check=False, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("apk.forward_failed", error=str(exc), command=cmd)
            return False
        if result.returncode != 0:
            log.warning(
                "apk.forward_failed",
                returncode=result.returncode,
                stderr=result.stderr.decode(errors="replace").strip(),
                command=cmd,
            )
            return False
        return True

    def _ensure_forward(self) -> None:
        if self._forwarded:
            return
        # Even a failed forward is only tried once per session: connect() below
        # may still succeed against a pre-existing forward, and if it can't the
        # poll simply returns None and the runner uses screencap.
        self._forwarded = True
        if self._forwarder():
            log.info(
                "apk.forwarded",
                port=self._port,
                socket=self._socket_name,
                serial=self._serial,
            )

    # -- link lifecycle -----------------------------------------------------

    def _ensure_link_locked(self) -> LinkLike | None:
        """Return a live link, connecting + starting its drain on first use.

        Caller must hold ``self._cond``. The connect targets the local ``adb
        forward`` port (effectively instant), so holding the lock across it is
        fine; the one slow path (the ``adb forward`` subprocess) runs at most
        once per session, before any drain thread exists.
        """
        if self._link is not None:
            return self._link
        self._ensure_forward()
        try:
            link = self._connector(self._port, self._host, self._read_timeout_s)
        except OSError as exc:
            # Connection refused / no route: the APK or forward is not up.
            log.warning("apk.connect_failed", error=str(exc), port=self._port)
            return None
        self._link = link
        log.info("apk.connected", port=self._port)
        threading.Thread(
            target=self._drain_loop,
            args=(link,),
            name=f"apk-drain-{self._port}",
            daemon=True,
        ).start()
        return link

    def _drop_locked(self) -> None:
        """Close and forget the current link so the next call reconnects.

        Caller must hold ``self._cond``. Closing under the lock keeps the
        ordering deterministic (the link is closed before any waiter observes
        the drop); waking the waiters means a ``poll`` blocked on the mailbox
        returns ``None`` promptly instead of sleeping out its full budget on a
        link that is already known dead.
        """
        link, self._link = self._link, None
        if link is not None:
            with contextlib.suppress(Exception):
                link.close()
        self._cond.notify_all()

    def start(self) -> None:
        """Eager, best-effort warm-up (forward + connect + drain). Never raises.

        Optional: :meth:`poll` / :meth:`latest` connect lazily too. Calling it
        up front surfaces a connection log (and sets up the forward) before the
        first move, and lets the background drain start banking packets while
        the session is still initializing.
        """
        with self._cond:
            self._ensure_link_locked()

    def close(self) -> None:
        """Release the link. Safe to call more than once; never raises.

        The background drain thread notices its link was dropped (identity
        check / the read failing on the closed socket) and exits on its own.
        """
        with self._cond:
            self._drop_locked()

    # -- background drain (producer side) ------------------------------------

    def _drain_loop(self, link: LinkLike) -> None:
        """Continuously read ``link`` and publish board packets to the mailbox.

        Runs on a daemon thread, one per link. Exits when the link errors /
        EOFs (dropping it so the consumer reconnects lazily) or when the link
        is no longer the source's current one (a reconnect or ``close``
        replaced it -- the stale reader must never touch the mailbox again).
        Control :class:`FramePacket` messages are skipped; a read timeout is a
        genuine lull (static board between heartbeats), not a dead link.
        """
        while True:
            with self._cond:
                if self._link is not link:
                    return  # replaced by reconnect / closed: stale reader exits
            try:
                message = link.read_message()
            except TimeoutError:
                continue  # no new packet within read_timeout_s: keep listening
            except OSError as exc:
                log.warning("apk.read_error", error=str(exc))
                self._drop_from(link)
                return
            if message is None:  # EOF: the device closed the stream
                log.warning("apk.link_eof")
                self._drop_from(link)
                return
            if not isinstance(message, BoardPacket):
                continue  # a FramePacket (periodic JPEG control frame): skip
            with self._cond:
                if self._link is not link:
                    return
                self._slot = (message, time.monotonic())
                self._gen += 1
                self._cond.notify_all()

    def _drop_from(self, link: LinkLike) -> None:
        """Drain-thread drop: forget ``link`` only if it is still current."""
        with self._cond:
            if self._link is link:
                self._drop_locked()
            else:
                # Already replaced; just make sure the dead socket is closed.
                with contextlib.suppress(Exception):
                    link.close()

    # -- reads (consumer side) ------------------------------------------------

    def _take_locked(self) -> BoardSample:
        """Consume the mailbox packet (caller holds the lock, ``gen`` advanced)."""
        assert self._slot is not None
        packet, received = self._slot
        self._consumed = self._gen
        return BoardSample.from_packet(
            packet, age_ms=max(0.0, (time.monotonic() - received) * 1000.0)
        )

    def last_packet_age_s(self) -> float | None:
        """Seconds since the drain thread last received ANY board packet.

        ``None`` when no packet has arrived yet this session. Read-only
        liveness telemetry for the escalation probe
        (:func:`bot2048.runner.escalation.probe_screen`): board packets still
        flowing prove the device link is ALIVE even while ``screencap`` is
        FLAG_SECURE-blind and ``uiautomator dump`` yields no hierarchy, so a
        blind probe must never conclude ``DEVICE_LOST``. Reads the mailbox
        timestamp WITHOUT consuming the packet (freshness for :meth:`poll`
        is untouched).
        """
        with self._cond:
            slot = self._slot
        if slot is None:
            return None
        _, received = slot
        return max(0.0, time.monotonic() - received)

    def poll(self) -> BoardSample | None:
        """The freshest unconsumed board, or ``None`` when the fast path has nothing.

        If the background drain already banked a packet the loop has not seen,
        it is returned IMMEDIATELY (this is the latency fix: the packet the
        phone pushed during engine search / swipe no longer waits for a socket
        read). Otherwise waits up to ``read_timeout_s`` -- the same blocking
        budget the old single-threaded socket read had -- for the next publish.
        Does NOT ping: right after a swipe the board is changing and the device
        emits packets on its own; a mid-animation board simply fails the
        caller's predict-gate and the next ``poll`` picks up the newer packet.
        A lull (rejected swipe, static board past the heartbeat) or a dropped
        link yields ``None`` and the caller falls back to a full frame.
        """
        with self._cond:
            if self._gen == self._consumed:
                # Nothing banked: make sure a link + drain exist, then wait.
                if self._ensure_link_locked() is None:
                    return None
                deadline = time.monotonic() + self._read_timeout_s
                while self._gen == self._consumed:
                    if self._link is None:
                        return None  # dropped mid-wait: no fast board
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None  # lull: no new packet within the budget
                    self._cond.wait(timeout=remaining)
            return self._take_locked()

    def send_swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int
    ) -> bool:
        """Ask the device to inject a swipe over the live sampler socket.

        Returns ``True`` when the :data:`~bot2048.ondevice.packet.CMD_SWIPE`
        command was written to a live link, ``False`` when no link is available
        or the write failed (link down mid-session) -- the caller then falls
        back to the ``input swipe`` shell path so a move is never dropped. The
        write reuses the SAME connection the board drain holds (the APK's
        ``LocalServerSocket`` accepts a single client, so a second connection
        would displace the capture stream); ``sendall`` is safe concurrently
        with the drain thread's blocking ``recv``.

        Fire-and-forget: this does not wait for the gesture to complete. The
        gesture takes ``duration_ms`` on the device; the caller
        (:class:`bot2048.device.apk_swipe.ApkSwipeInjector`) blocks for that
        span to preserve the old ``input swipe`` timing, and the runner's
        board-settle gate confirms the move landed.
        """
        with self._cond:
            link = self._ensure_link_locked()
            if link is None:
                return False
            try:
                link.swipe(x1, y1, x2, y2, duration_ms)
            except OSError as exc:
                log.warning("apk.swipe_send_failed", error=str(exc))
                self._drop_locked()
                return False
            return True

    def latest(self) -> BoardSample | None:
        """The freshest board right now: ping, then settle to the newest packet.

        For one-shot "what is on the board" reads (liveness probe, capture-only
        checks) rather than the per-move gate. Pings so a static board still
        answers, then waits for the reply and lets a burst quiesce
        (:data:`_LATEST_QUIESCE_S`) so the newest packet of the burst -- the
        settled board rather than a mid-animation one -- is returned. ``None``
        when nothing arrives within ``read_timeout_s`` (silent/dead link).
        """
        with self._cond:
            link = self._ensure_link_locked()
            if link is None:
                return None
            with contextlib.suppress(OSError):
                link.ping()
            deadline = time.monotonic() + self._read_timeout_s
            while self._gen == self._consumed:
                if self._link is None:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
            # A packet landed; drain the burst: keep waiting while strictly
            # newer publishes arrive within the quiescence window.
            seen = self._gen
            while self._link is not None:
                self._cond.wait(timeout=_LATEST_QUIESCE_S)
                if self._gen == seen:
                    break  # quiet for a full window: the burst settled
                seen = self._gen
            return self._take_locked()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

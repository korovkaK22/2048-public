"""scrcpy-server raw H.264 frame source -- the fast capture path on this device.

minicap is dead on Android 13 / SDK 33 (no compatible ``minicap.so``; see
docs/live-setup.md). The verified replacement: scrcpy-server in
``raw_stream`` mode emits a **pure H.264 elementary stream** on an abstract
socket (``localabstract:scrcpy_<scid>``), reached via
``adb forward tcp:<port>``. The bring-up agent verified 120 frames decoded
at native 1080x2400, pixel-identical to ``screencap`` -- at stream latency
(~tens of ms) instead of ~0.65 s per ``screencap`` grab.

Pipeline (all host-side; the server jar is ALREADY pushed to
``/data/local/tmp/scrcpy-server`` -- this module installs nothing)::

    scrcpy-server --(abstract socket)--> adb forward --(TCP)--> feeder thread
        --> ffmpeg stdin (H.264) ... ffmpeg stdout (rawvideo rgb24)
        --> reader thread --> latest-frame buffer --> capture()

Decoding uses a host ``ffmpeg`` subprocess (on PATH; pyav is NOT installed):
the feeder thread pumps socket bytes into ffmpeg's stdin, the reader thread
slices ffmpeg's stdout into exactly W*H*3-byte rgb24 frames and keeps only
the NEWEST one. ``capture()`` returns that latest frame immediately -- the
win over screencap is that a recent frame is *continuously* available.

Freshness caveats (from docs/live-setup.md + elementary-stream parsing):

* the server only emits frames when the screen changes (+ periodic
  keyframes) -- a static board sends almost nothing, which is fine because
  the latest buffered frame IS the current screen;
* ffmpeg's demux/parse pipeline holds the newest ~2 access units until
  further bytes (or EOF) arrive (raw elementary streams carry no per-frame
  framing; measured with the exact decode command on canned clips), so
  worst-case staleness is ~2 frame intervals -- the live validation should
  confirm a swipe shows up promptly.

AUTH caveat: under ``FLAG_SECURE`` (the monobank auth wall) scrcpy streams
all-BLACK frames, not an error. The 0-byte ``screencap`` probe
(:meth:`bot2048.device.capture.ScreencapSource.is_auth_blocked`) remains the
canonical AUTH_REQUIRED sensor. As a secondary guard so the streaming loop
does not silently feed black pixels to vision, this source counts consecutive
all-black decoded frames and, once ``config.black_frame_limit`` is reached,
raises :class:`ScrcpySecureError` from :meth:`ScrcpySource.capture` -- a hard,
non-retried signal for the runner to fall back to ``screencap``. The 2048
board and monobank home are never all-black, and a single non-black frame
resets the counter, so a lone transient black frame cannot trip it.

The adb side (server spawn, port forward, teardown) is behind the
:class:`ScrcpyRunner` protocol so unit tests inject a fake and serve a
canned H.264 clip from an in-process TCP server -- no device, no adb needed.
"""

from __future__ import annotations

import contextlib
import re
import socket
import subprocess
import threading
import time
from types import TracebackType
from typing import IO, Protocol, Self, cast

import numpy as np
import numpy.typing as npt
import structlog

from bot2048.device.adb import DeviceError
from bot2048.device.capture import Image, ScrcpyConfig

log = structlog.get_logger(__name__)

__all__ = [
    "AdbScrcpyRunner",
    "ScrcpyConnectionError",
    "ScrcpyError",
    "ScrcpyRunner",
    "ScrcpySecureError",
    "ScrcpySource",
]

#: Socket read chunk for the feeder thread.
_RECV_CHUNK: int = 65536

#: How long to wait for pipeline threads/processes to die during teardown.
_TEARDOWN_TIMEOUT_S: float = 5.0

#: ``WxH`` extractor for ffmpeg's ``Stream #...: Video: ...`` stderr lines.
_GEOMETRY_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")


class ScrcpyError(DeviceError):
    """scrcpy capture failure (start, decode pipeline, retries exhausted)."""


class ScrcpyConnectionError(ScrcpyError):
    """The stream/decoder pipeline dropped or produced no frame -- reconnectable."""


class ScrcpySecureError(ScrcpyError):
    """A sustained run of all-black frames -- a ``FLAG_SECURE`` window is suspected.

    Under the monobank auth wall the compositor excludes secure layers from
    the scrcpy capture, so the stream goes BLACK instead of erroring (see the
    module docstring + docs/live-setup.md). This is NOT reconnectable and must
    NOT be retried -- relaunching the server against a secure screen just
    reproduces black. It is a hard signal for the runner to fall back to the
    ``screencap`` 0-byte AUTH sensor
    (:meth:`bot2048.device.capture.ScreencapSource.is_auth_blocked`). The
    source stays "secure" until :meth:`ScrcpySource.start` is called on a fresh
    pipeline (e.g. after ``close()``), giving the caller explicit control over
    when to re-probe.
    """


def _is_black_frame(frame: Image, max_value: int) -> bool:
    """``True`` when every channel of every pixel is ``<= max_value``.

    Pure + synthetic-testable (no ffmpeg/device). ``frame.max()`` over a
    contiguous rgb24 buffer is a single fast pass.
    """
    return bool(frame.size) and int(frame.max()) <= max_value


class ScrcpyRunner(Protocol):
    """The adb-side lifecycle of scrcpy-server (injectable for tests).

    Implementations start/stop the on-device server process and manage the
    TCP forward; they do NOT touch the stream socket or ffmpeg.
    """

    def start_server(self, shell_command: str) -> None:
        """Launch scrcpy-server via ``adb shell <shell_command>`` (non-blocking)."""

    def forward(self, local_port: int, socket_name: str) -> None:
        """Set up ``adb forward tcp:<local_port> localabstract:<socket_name>``."""

    def remove_forward(self, local_port: int) -> None:
        """Remove the forward (best-effort)."""

    def stop_server(self) -> None:
        """Kill the on-device scrcpy-server process (best-effort)."""


class AdbScrcpyRunner:
    """Default :class:`ScrcpyRunner`: drives the host ``adb`` binary.

    ASSUMPTION to satisfy before live bring-up: the
    scrcpy-server jar matching ``ScrcpyConfig.server_version`` is already
    pushed to ``ScrcpyConfig.server_path`` (``scripts/setup_adb.sh`` +
    ``adb push tools/scrcpy/scrcpy-server /data/local/tmp/scrcpy-server``,
    verified with scrcpy v4.0). This class installs nothing.

    In this repo adb lives at ``project/tools/platform-tools/adb.exe``
    (gitignored) -- pass that path as ``adb_exe``. Note: ``adb`` is spawned
    directly (no shell), so git-bash's ``/data/...`` path mangling
    (``MSYS_NO_PATHCONV``) does not apply here.
    """

    def __init__(self, *, adb_exe: str = "adb", serial: str | None = None) -> None:
        self.adb_exe = adb_exe
        self.serial = serial
        self._proc: subprocess.Popen[bytes] | None = None

    def _adb(self, *args: str) -> list[str]:
        cmd = [self.adb_exe]
        if self.serial is not None:
            cmd += ["-s", self.serial]
        cmd += list(args)
        return cmd

    def start_server(self, shell_command: str) -> None:
        self._proc = subprocess.Popen(
            self._adb("shell", shell_command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("scrcpy.server_spawned", command=shell_command, serial=self.serial)

    def forward(self, local_port: int, socket_name: str) -> None:
        subprocess.run(
            self._adb("forward", f"tcp:{local_port}", f"localabstract:{socket_name}"),
            check=True,
            capture_output=True,
            timeout=15,
        )
        log.info("scrcpy.forward_set", local_port=local_port, socket_name=socket_name)

    def remove_forward(self, local_port: int) -> None:
        subprocess.run(
            self._adb("forward", "--remove", f"tcp:{local_port}"),
            check=False,
            capture_output=True,
            timeout=15,
        )

    def stop_server(self) -> None:
        subprocess.run(
            self._adb("shell", "pkill -f com.genymobile.scrcpy"),
            check=False,
            capture_output=True,
            timeout=15,
        )
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.kill()
            self._proc = None


class ScrcpySource:
    """Fast :class:`~bot2048.device.capture.FrameSource`: scrcpy H.264 + ffmpeg.

    Lifecycle: :meth:`start` sets the forward, launches scrcpy-server on the
    device, connects the TCP stream socket, spawns the host ffmpeg decoder
    and starts the feeder/reader threads. Every :meth:`capture` then returns
    the newest decoded frame (RGB uint8 ``(H, W, 3)``), waiting up to
    ``config.first_frame_timeout_s`` on a fresh pipeline.

    Robustness: the server exits when the client disconnects, so any
    pipeline death (socket EOF, ffmpeg exit, geometry mismatch) marks the
    pipeline dead and the next :meth:`capture` relaunches the server and
    rebuilds socket + decoder, up to ``config.reconnect_attempts`` extra
    tries. Cannot detect AUTH (see module docs) -- pair with
    ``ScreencapSource.is_auth_blocked``.
    """

    def __init__(
        self,
        config: ScrcpyConfig | None = None,
        *,
        runner: ScrcpyRunner | None = None,
    ) -> None:
        self.config = config or ScrcpyConfig()
        self._runner: ScrcpyRunner = (
            runner
            if runner is not None
            else AdbScrcpyRunner(serial=self.config.device_serial)
        )
        self._sock: socket.socket | None = None
        self._ffmpeg: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._latest: Image | None = None
        self._latest_lock = threading.Lock()
        self._frame_ready = threading.Event()
        self._dead = threading.Event()
        self._dead.set()  # no pipeline yet
        self._secure = threading.Event()  # N consecutive black frames seen
        self._consecutive_black = 0  # reader-thread-only counter
        self._pipeline_error: str | None = None
        self._frames_decoded = 0
        self._started = False
        self._closing = False

    # -- introspection ---------------------------------------------------------

    @property
    def server_command(self) -> str:
        """The verified ``adb shell`` launch line (docs/live-setup.md recipe).

        With ``config.crop`` set, appends the server-side ``crop=W:H:X:Y``
        argument (screen-space px, applied before ``max_size`` scaling; the
        v4.0 jar's ``parseCrop`` expects exactly 4 colon-separated values).
        The stream geometry then equals the crop size -- board-only frames
        for :mod:`bot2048.device.crop`.
        """
        cfg = self.config
        crop_arg = ""
        if cfg.crop is not None:
            x, y, width, height = cfg.crop
            crop_arg = f" crop={width}:{height}:{x}:{y}"
        return (
            f"CLASSPATH={cfg.server_path} app_process / com.genymobile.scrcpy.Server "
            f"{cfg.server_version} scid={cfg.scid} log_level=info "
            f"audio=false control=false cleanup=false "
            f"raw_stream=true send_dummy_byte=false max_size=0 video_codec=h264 "
            f"tunnel_forward=true video_bit_rate={cfg.video_bit_rate} max_fps={cfg.max_fps}"
            f"{crop_arg}"
        )

    @property
    def ffmpeg_command(self) -> list[str]:
        """Host decode command: H.264 elementary stream in, rawvideo rgb24 out."""
        cfg = self.config
        return [
            cfg.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-loglevel", "info",
            "-probesize", "32",
            "-flags", "low_delay",
            # NO "-fflags nobuffer": it DISCARDS the packets consumed while
            # probing (measured: first 1-2 frames lost; a held-open pipe
            # emitted nothing at all). Without it probed packets are
            # re-emitted, so decode is lossless at EOF.
            "-f", "h264",
            "-i", "pipe:0",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-flush_packets", "1",  # write each frame immediately (no avio buffering)
            "pipe:1",
        ]

    @property
    def pipeline_alive(self) -> bool:
        """``True`` while socket + ffmpeg + threads are believed healthy."""
        proc = self._ffmpeg
        return (
            self._sock is not None
            and proc is not None
            and proc.poll() is None
            and not self._dead.is_set()
        )

    @property
    def frames_decoded(self) -> int:
        """Total frames decoded since :meth:`start` (cumulative across reconnects)."""
        with self._latest_lock:
            return self._frames_decoded

    @property
    def secure_detected(self) -> bool:
        """``True`` once ``config.black_frame_limit`` consecutive all-black frames
        were decoded -- a suspected ``FLAG_SECURE`` window. Sticky until a fresh
        :meth:`start`; the next :meth:`capture` raises :class:`ScrcpySecureError`."""
        return self._secure.is_set()

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Forward + server launch + socket + ffmpeg + threads. Idempotent."""
        if self._started:
            return
        self._started = True  # so close() cleans up after a partial start
        cfg = self.config
        try:
            self._runner.forward(cfg.local_port, cfg.socket_name)
            self._runner.start_server(self.server_command)
            self._connect_pipeline()
        except ScrcpyError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise ScrcpyError(f"failed to start scrcpy pipeline: {exc}") from exc
        log.info(
            "scrcpy.started",
            local_port=cfg.local_port,
            socket_name=cfg.socket_name,
            size=cfg.frame_size,
            crop=cfg.crop,
            serial=cfg.device_serial,
        )

    def close(self) -> None:
        """Tear everything down: threads, ffmpeg, socket, server, forward."""
        self._teardown_pipeline()
        if not self._started:
            return
        self._started = False
        try:
            self._runner.stop_server()
        except Exception as exc:  # cleanup is best-effort; runner is injectable
            log.warning("scrcpy.stop_failed", error=str(exc))
        try:
            self._runner.remove_forward(self.config.local_port)
        except Exception as exc:
            log.warning("scrcpy.remove_forward_failed", error=str(exc))
        with self._latest_lock:
            self._latest = None
        log.info("scrcpy.closed")

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

    # -- capture ---------------------------------------------------------------

    def capture(self) -> Image:
        """Newest decoded frame as RGB uint8 ``(H, W, 3)``.

        On a healthy pipeline with at least one decoded frame this returns
        immediately (the latest frame IS the current screen -- the server
        pushes on change). On a fresh/reconnected pipeline it waits up to
        ``config.first_frame_timeout_s`` for the first decode. A dead
        pipeline is rebuilt (server relaunch included) before waiting.

        Raises:
            ScrcpySecureError: a suspected ``FLAG_SECURE`` window (a sustained
                all-black run). NOT retried -- the runner should fall back to
                the ``screencap`` AUTH sensor. Sticky until a fresh
                :meth:`start`.
            ScrcpyError: not started, or the pipeline kept dying through
                all reconnect attempts.
        """
        if not self._started:
            raise ScrcpyError(
                "ScrcpySource is not started -- call start() or use it as a context manager"
            )
        cfg = self.config
        attempts = cfg.reconnect_attempts + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self._raise_if_secure()  # cheap sticky check; never rebuild a secure pipeline
                if not self.pipeline_alive:
                    log.warning(
                        "scrcpy.reconnecting", attempt=attempt, reason=self._pipeline_error
                    )
                    self._restart_pipeline()
                if self._frame_ready.wait(cfg.first_frame_timeout_s):
                    self._raise_if_secure()  # a black run may have tripped during the wait
                    with self._latest_lock:
                        frame = self._latest
                    if frame is not None:
                        return frame
                raise ScrcpyConnectionError(
                    f"no decoded frame within {cfg.first_frame_timeout_s}s "
                    f"(pipeline_error={self._pipeline_error})"
                )
            except ScrcpySecureError:
                self._teardown_pipeline()  # stop fighting the secure screen; stay sticky
                raise
            except ScrcpyError as exc:
                last_error = exc
                self._teardown_pipeline()
                log.warning(
                    "scrcpy.capture_attempt_failed", attempt=attempt, error=str(exc)
                )
        raise ScrcpyError(f"scrcpy capture failed after {attempts} attempts: {last_error}")

    def _raise_if_secure(self) -> None:
        """Raise :class:`ScrcpySecureError` if the black-frame limit tripped."""
        if self._secure.is_set():
            raise ScrcpySecureError(
                f"{self.config.black_frame_limit}+ consecutive all-black frames "
                f"(<= {self.config.black_pixel_max}/255) -- FLAG_SECURE window suspected; "
                f"fall back to screencap (the 0-byte AUTH sensor)"
            )

    # -- pipeline internals ------------------------------------------------------

    def _connect_pipeline(self) -> None:
        """Socket + ffmpeg + threads for one pipeline generation."""
        self._frame_ready.clear()
        self._secure.clear()
        self._consecutive_black = 0
        self._pipeline_error = None
        with self._latest_lock:
            self._latest = None
        sock = self._connect_socket()
        self._sock = sock
        try:
            proc = self._spawn_ffmpeg()
        except ScrcpyError:
            self._close_socket()
            raise
        self._ffmpeg = proc
        self._closing = False
        self._dead.clear()
        self._threads = [
            threading.Thread(
                target=self._feed_loop, args=(sock, proc), name="scrcpy-feed", daemon=True
            ),
            threading.Thread(
                target=self._read_loop, args=(proc,), name="scrcpy-read", daemon=True
            ),
            threading.Thread(
                target=self._stderr_loop, args=(proc,), name="scrcpy-stderr", daemon=True
            ),
        ]
        for thread in self._threads:
            thread.start()

    def _connect_socket(self) -> socket.socket:
        """Connect to the forwarded port and wait for the first stream byte.

        adb accepts the TCP connection even before the abstract socket
        exists and then closes it immediately, so a bare ``connect`` is not
        proof of a live server -- peek one byte (MSG_PEEK, nothing consumed)
        before declaring victory. With ``send_dummy_byte=false`` the first
        bytes are the H.264 SPS, sent as soon as the encoder starts.
        """
        cfg = self.config
        last_error: Exception | None = None
        for attempt in range(1, cfg.connect_attempts + 1):
            sock: socket.socket | None = None
            try:
                sock = socket.create_connection(
                    ("127.0.0.1", cfg.local_port), timeout=cfg.connect_timeout_s
                )
                sock.settimeout(cfg.connect_timeout_s)
                probe = sock.recv(1, socket.MSG_PEEK)
                if not probe:
                    raise ScrcpyConnectionError(
                        "forward connected but stream closed immediately "
                        "(scrcpy-server not listening yet?)"
                    )
                sock.settimeout(None)  # feeder uses blocking reads; close() unblocks
                return sock
            except (OSError, ScrcpyConnectionError) as exc:
                last_error = exc
                if sock is not None:
                    with contextlib.suppress(OSError):
                        sock.close()
            if attempt < cfg.connect_attempts:
                time.sleep(cfg.connect_retry_delay_s)
        raise ScrcpyError(
            f"cannot connect to scrcpy stream at 127.0.0.1:{cfg.local_port} "
            f"after {cfg.connect_attempts} attempts: {last_error}"
        )

    def _spawn_ffmpeg(self) -> subprocess.Popen[bytes]:
        command = self.ffmpeg_command
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ScrcpyError(
                f"cannot spawn ffmpeg ({self.config.ffmpeg_path!r} -- is it on PATH?): {exc}"
            ) from exc
        log.info("scrcpy.ffmpeg_spawned", command=command, pid=proc.pid)
        return proc

    def _restart_pipeline(self) -> None:
        """Full rebuild: the server exits on client disconnect, so relaunch it."""
        self._teardown_pipeline()
        try:
            self._runner.stop_server()  # belt & braces before relaunch
        except Exception as exc:
            log.warning("scrcpy.stop_before_restart_failed", error=str(exc))
        try:
            self._runner.start_server(self.server_command)
        except Exception as exc:
            raise ScrcpyError(f"cannot relaunch scrcpy-server: {exc}") from exc
        self._connect_pipeline()

    def _teardown_pipeline(self) -> None:
        """Stop threads + ffmpeg + socket of the current generation (idempotent)."""
        self._closing = True  # hush pipeline_dead warnings caused by our own teardown
        self._dead.set()
        self._close_socket()  # unblocks the feeder's recv
        proc = self._ffmpeg
        self._ffmpeg = None
        if proc is not None:
            for stream in (proc.stdin,):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()
            if proc.poll() is None:
                proc.kill()  # unblocks the reader's stdout read (EOF)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_TEARDOWN_TIMEOUT_S)
        threads = self._threads
        self._threads = []
        for thread in threads:
            thread.join(timeout=_TEARDOWN_TIMEOUT_S)
            if thread.is_alive():
                log.warning("scrcpy.thread_join_timeout", thread=thread.name)
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()

    def _close_socket(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def _mark_dead(self, reason: str) -> None:
        if self._pipeline_error is None:
            self._pipeline_error = reason
        self._dead.set()
        if not self._closing:
            log.warning("scrcpy.pipeline_dead", reason=reason)

    # -- pipeline threads --------------------------------------------------------

    def _feed_loop(self, sock: socket.socket, proc: subprocess.Popen[bytes]) -> None:
        """Pump raw H.264 bytes: device socket -> ffmpeg stdin."""
        stdin = proc.stdin
        if stdin is None:  # pragma: no cover - Popen(stdin=PIPE) guarantees it
            self._mark_dead("ffmpeg has no stdin pipe")
            return
        try:
            while True:
                chunk = sock.recv(_RECV_CHUNK)
                if not chunk:
                    self._mark_dead("device stream EOF (scrcpy-server exited?)")
                    break
                stdin.write(chunk)
                stdin.flush()
        except (OSError, ValueError) as exc:  # socket closed / ffmpeg pipe broke
            self._mark_dead(f"feeder stopped: {exc}")
        finally:
            # Closing stdin makes ffmpeg flush any buffered frames and exit,
            # which in turn gives the reader thread a clean EOF.
            with contextlib.suppress(OSError, ValueError):
                stdin.close()

    def _read_loop(self, proc: subprocess.Popen[bytes]) -> None:
        """Slice ffmpeg stdout into rgb24 frames; keep only the newest."""
        stdout = proc.stdout
        if stdout is None:  # pragma: no cover - Popen(stdout=PIPE) guarantees it
            self._mark_dead("ffmpeg has no stdout pipe")
            return
        width, height = self.config.frame_size
        frame_bytes = self.config.frame_bytes
        try:
            while True:
                data = self._read_exact(stdout, frame_bytes)
                if data is None:
                    self._mark_dead("ffmpeg stdout EOF")
                    break
                frame = cast(
                    npt.NDArray[np.uint8],
                    np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3),
                )
                with self._latest_lock:
                    self._latest = frame
                    self._frames_decoded += 1
                self._frame_ready.set()
                if self._check_black(frame):
                    break  # secure window: stop decoding, let capture() surface it
        except (OSError, ValueError) as exc:
            self._mark_dead(f"reader stopped: {exc}")

    def _check_black(self, frame: Image) -> bool:
        """Track consecutive black frames; trip the secure flag at the limit.

        Reader-thread-only (owns ``_consecutive_black``). Returns ``True`` when
        the limit is reached so the caller stops the read loop -- there is no
        point decoding more black frames from a secure screen.
        """
        limit = self.config.black_frame_limit
        if limit <= 0:
            return False
        if _is_black_frame(frame, self.config.black_pixel_max):
            self._consecutive_black += 1
            if self._consecutive_black >= limit:
                self._secure.set()
                self._frame_ready.set()  # wake a capture() blocked on the first frame
                self._mark_dead(
                    f"{self._consecutive_black} consecutive all-black frames "
                    f"(<= {self.config.black_pixel_max}/255) -- FLAG_SECURE suspected"
                )
                return True
        else:
            self._consecutive_black = 0
        return False

    def _read_exact(self, stream: IO[bytes], n: int) -> bytearray | None:
        """Read exactly ``n`` bytes; ``None`` on EOF (partial tail is logged).

        Returns a fresh ``bytearray`` so the resulting ndarray is writable
        without an extra copy (``np.frombuffer`` over ``bytes`` is read-only).
        """
        buf = bytearray()
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                if buf and not self._closing:
                    log.debug("scrcpy.partial_frame_dropped", got=len(buf), expected=n)
                return None
            buf += chunk
        return buf

    def _stderr_loop(self, proc: subprocess.Popen[bytes]) -> None:
        """Log ffmpeg stderr; cross-check stream geometry against config.

        The first ``Video:`` stream line carries the parsed input geometry
        (e.g. ``... Video: h264 (High), yuv420p, 1080x2400, ...``). A
        mismatch with ``config.frame_size`` (the crop size when cropping,
        the full screen otherwise) means every stdout slice is misframed
        garbage -- kill the pipeline instead of feeding bad pixels to vision.
        """
        stderr = proc.stderr
        if stderr is None:  # pragma: no cover - Popen(stderr=PIPE) guarantees it
            return
        width, height = self.config.frame_size
        geometry_checked = False
        try:
            for raw in iter(stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if not geometry_checked and "Video:" in line:
                    match = _GEOMETRY_RE.search(line)
                    if match is not None:
                        geometry_checked = True
                        got = (int(match.group(1)), int(match.group(2)))
                        if got != (width, height):
                            self._mark_dead(
                                f"stream geometry {got[0]}x{got[1]} != configured "
                                f"{width}x{height} -- fix ScrcpyConfig.size/crop"
                            )
                        else:
                            log.info("scrcpy.geometry_confirmed", size=got)
                log.debug("scrcpy.ffmpeg_stderr", line=line)
        except (OSError, ValueError):  # pipe closed during teardown
            return

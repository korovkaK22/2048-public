"""Minicap-based frame source -- the PRIMARY capture path for the live loop.

minicap (openstf/DeviceFarmer) runs on the device and streams JPEG frames
over an abstract UNIX socket, which we reach through
``adb forward tcp:<local_port> localabstract:minicap``. Compared to a fresh
``screencap -p`` per move this removes the process spawn + full PNG encode
round-trip, cutting capture latency to a socket read + JPEG decode.

Wire protocol (minicap README):

* one **global banner** per connection, 24 bytes:
  ``[0]`` version (must be 1), ``[1]`` banner size, ``[2:6]`` pid (u32 LE),
  ``[6:10]`` real width, ``[10:14]`` real height, ``[14:18]`` virtual width,
  ``[18:22]`` virtual height (all u32 LE), ``[22]`` orientation,
  ``[23]`` quirk bitflags;
* then frames forever: a u32 LE byte length followed by exactly that many
  bytes of JPEG.

AUTH caveat (docs/device-ui-notes.md): on ``FLAG_SECURE`` screens (the
monobank auth wall) minicap streams plain BLACK frames -- **not an error** --
so it cannot detect AUTH_REQUIRED. The 0-byte ``screencap`` probe
(:meth:`bot2048.device.capture.ScreencapSource.is_auth_blocked`) stays the
canonical auth sensor.

The adb side (process start, port forward, teardown) is behind the
:class:`MinicapRunner` protocol so unit tests inject a fake and talk to an
in-process TCP server -- no device, no adb binary needed.
"""

from __future__ import annotations

import contextlib
import io
import select
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self, cast

import numpy as np
import numpy.typing as npt
import structlog

from bot2048.device.adb import DeviceError, _import_pil_image
from bot2048.device.capture import Image, MinicapConfig

log = structlog.get_logger(__name__)

__all__ = [
    "BANNER_SIZE",
    "AdbMinicapRunner",
    "MinicapBanner",
    "MinicapBannerError",
    "MinicapConnectionError",
    "MinicapError",
    "MinicapRunner",
    "MinicapSource",
]

#: Size of the fixed part of the global banner, bytes.
BANNER_SIZE: int = 24

#: Banner wire layout: version, banner size, pid, real w/h, virtual w/h,
#: orientation, quirks -- all little-endian.
_BANNER_STRUCT = struct.Struct("<BBIIIIIBB")

#: Frame length sanity cap -- a JPEG of a phone screen is well under this;
#: anything bigger means we lost framing (reading pixels as a length).
_MAX_FRAME_BYTES: int = 32 * 1024 * 1024

#: Safety bound for the freshness drain loop in one capture() call.
_MAX_DRAIN_FRAMES: int = 120


class MinicapError(DeviceError):
    """Minicap capture failure (start, protocol, decode, retries exhausted)."""


class MinicapBannerError(MinicapError):
    """The global banner is malformed or has an unsupported version."""


class MinicapConnectionError(MinicapError):
    """The stream socket dropped mid-read (EOF/timeout) -- reconnectable."""


@dataclass(frozen=True, slots=True)
class MinicapBanner:
    """Parsed 24-byte global banner minicap sends once per connection."""

    version: int
    banner_size: int
    pid: int
    real_width: int
    real_height: int
    virtual_width: int
    virtual_height: int
    orientation: int
    quirks: int

    @classmethod
    def parse(cls, data: bytes) -> MinicapBanner:
        """Parse the fixed 24-byte banner prefix; validate ``version == 1``."""
        if len(data) != BANNER_SIZE:
            raise MinicapBannerError(
                f"banner must be exactly {BANNER_SIZE} bytes, got {len(data)}"
            )
        banner = cls(*_BANNER_STRUCT.unpack(data))
        if banner.version != 1:
            raise MinicapBannerError(
                f"unsupported minicap banner version {banner.version} (expected 1)"
            )
        if banner.banner_size < BANNER_SIZE:
            raise MinicapBannerError(
                f"banner_size {banner.banner_size} < fixed part {BANNER_SIZE}"
            )
        return banner


class MinicapRunner(Protocol):
    """The adb-side lifecycle of minicap (injectable for tests).

    Implementations start/stop the on-device process and manage the TCP
    forward; they do NOT touch the stream socket itself.
    """

    def start_minicap(self, projection: str) -> None:
        """Launch minicap on the device with ``-P <projection>`` (non-blocking)."""

    def forward(self, local_port: int) -> None:
        """Set up ``adb forward tcp:<local_port> localabstract:minicap``."""

    def remove_forward(self, local_port: int) -> None:
        """Remove the forward (best-effort)."""

    def stop_minicap(self) -> None:
        """Kill the on-device minicap process (best-effort)."""


class AdbMinicapRunner:
    """Default :class:`MinicapRunner`: drives the host ``adb`` binary.

    ASSUMPTION to satisfy before live bring-up: the
    ``minicap`` executable and matching ``minicap.so`` for this device's
    ABI + Android SDK level are already pushed to ``bin_dir`` (default
    ``/data/local/tmp``) and chmod +x. This class installs nothing.

    In this repo adb lives at ``project/tools/platform-tools/adb.exe``
    (gitignored) -- pass that path as ``adb_exe``.
    """

    def __init__(
        self,
        *,
        adb_exe: str = "adb",
        serial: str | None = None,
        bin_dir: str = "/data/local/tmp",
    ) -> None:
        self.adb_exe = adb_exe
        self.serial = serial
        self.bin_dir = bin_dir
        self._proc: subprocess.Popen[bytes] | None = None

    def _adb(self, *args: str) -> list[str]:
        cmd = [self.adb_exe]
        if self.serial is not None:
            cmd += ["-s", self.serial]
        cmd += list(args)
        return cmd

    def start_minicap(self, projection: str) -> None:
        shell_cmd = f"LD_LIBRARY_PATH={self.bin_dir} {self.bin_dir}/minicap -P {projection}"
        self._proc = subprocess.Popen(
            self._adb("shell", shell_cmd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("minicap.process_spawned", command=shell_cmd, serial=self.serial)

    def forward(self, local_port: int) -> None:
        subprocess.run(
            self._adb("forward", f"tcp:{local_port}", "localabstract:minicap"),
            check=True,
            capture_output=True,
            timeout=15,
        )
        log.info("minicap.forward_set", local_port=local_port)

    def remove_forward(self, local_port: int) -> None:
        subprocess.run(
            self._adb("forward", "--remove", f"tcp:{local_port}"),
            check=False,
            capture_output=True,
            timeout=15,
        )

    def stop_minicap(self) -> None:
        subprocess.run(
            self._adb("shell", "pkill -f minicap"),
            check=False,
            capture_output=True,
            timeout=15,
        )
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.kill()
            self._proc = None


class MinicapSource:
    """PRIMARY :class:`~bot2048.device.capture.FrameSource`: minicap stream.

    Lifecycle: :meth:`start` launches minicap on the device (projection
    ``<realW>x<realH>@<virtW>x<virtH>/<rot>``; the default virtual size is
    the real size -- 1:1, so the 1080x2400 vision calibration stays valid),
    sets the TCP forward, connects and validates the banner. Then every
    :meth:`capture` reads the next length-prefixed JPEG, drains any frames
    already buffered in the socket (so the RETURNED frame is the newest one)
    and decodes it to RGB uint8 ``(H, W, 3)``.

    Robustness: a dropped/timed-out stream closes the socket and the next
    attempt re-runs the connect + banner handshake, up to
    ``config.reconnect_attempts`` extra tries per :meth:`capture` call.
    Cannot detect AUTH (see module docs) -- pair with
    ``ScreencapSource.is_auth_blocked``.
    """

    def __init__(
        self,
        config: MinicapConfig | None = None,
        *,
        runner: MinicapRunner | None = None,
    ) -> None:
        self.config = config or MinicapConfig()
        self._runner: MinicapRunner = (
            runner
            if runner is not None
            else AdbMinicapRunner(
                serial=self.config.device_serial, bin_dir=self.config.bin_dir
            )
        )
        self._sock: socket.socket | None = None
        self._banner: MinicapBanner | None = None
        self._started = False

    @property
    def banner(self) -> MinicapBanner | None:
        """Banner of the CURRENT connection (``None`` before start/after close)."""
        return self._banner

    @property
    def projection(self) -> str:
        """The ``-P`` argument: ``<realW>x<realH>@<virtW>x<virtH>/<rot>``."""
        rw, rh = self.config.real_size
        vw, vh = self.config.effective_virtual_size
        return f"{rw}x{rh}@{vw}x{vh}/{self.config.rotation}"

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start minicap + forward + connect + banner handshake. Idempotent."""
        if self._started:
            return
        projection = self.projection
        self._started = True  # so close() cleans up after a partial start
        try:
            self._runner.start_minicap(projection)
            self._runner.forward(self.config.local_port)
            self._connect()
        except MinicapError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise MinicapError(
                f"failed to start minicap (projection={projection}): {exc}"
            ) from exc
        log.info(
            "minicap.started",
            projection=projection,
            local_port=self.config.local_port,
            serial=self.config.device_serial,
        )

    def close(self) -> None:
        """Tear everything down: socket, on-device process, forward."""
        self._close_socket()
        if not self._started:
            return
        self._started = False
        try:
            self._runner.stop_minicap()
        except Exception as exc:  # cleanup is best-effort; runner is injectable
            log.warning("minicap.stop_failed", error=str(exc))
        try:
            self._runner.remove_forward(self.config.local_port)
        except Exception as exc:
            log.warning("minicap.remove_forward_failed", error=str(exc))
        self._banner = None
        log.info("minicap.closed")

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

    # -- capture -------------------------------------------------------------

    def capture(self) -> Image:
        """Freshest full frame as RGB uint8 ``(H, W, 3)``.

        Reads the next frame off the stream, then drains whatever complete
        frames are already buffered so a slow caller never gets a stale
        image. On a stream drop the socket is re-established (fresh banner;
        minicap pushes the current frame to every new client, which also
        makes a ``read_timeout_s`` on a static screen self-healing).

        Raises:
            MinicapError: not started, framing/decode error, or the stream
                kept dropping through all reconnect attempts.
        """
        if not self._started:
            raise MinicapError(
                "MinicapSource is not started -- call start() or use it as a context manager"
            )
        attempts = self.config.reconnect_attempts + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                if self._sock is None:
                    log.warning("minicap.reconnecting", attempt=attempt)
                    self._connect()
                frame = self._read_frame()
                frame = self._drain_to_latest(frame)
            except (OSError, MinicapConnectionError) as exc:
                last_error = exc
                self._close_socket()
                log.warning("minicap.frame_read_failed", attempt=attempt, error=str(exc))
            else:
                return self._decode_frame(frame)
        raise MinicapError(
            f"minicap capture failed after {attempts} attempts: {last_error}"
        )

    # -- internals -------------------------------------------------------------

    def _connect(self) -> None:
        """Connect to the forwarded port and read the banner, with retries.

        :class:`MinicapBannerError` is NOT retried -- a bad banner means a
        wrong/foreign process is answering, retrying cannot help.
        """
        cfg = self.config
        last_error: Exception | None = None
        for attempt in range(1, cfg.connect_attempts + 1):
            try:
                sock = socket.create_connection(
                    ("127.0.0.1", cfg.local_port), timeout=cfg.connect_timeout_s
                )
            except OSError as exc:
                last_error = exc
            else:
                sock.settimeout(cfg.read_timeout_s)
                self._sock = sock
                try:
                    self._read_banner()
                except MinicapBannerError:
                    self._close_socket()
                    raise
                except (OSError, MinicapConnectionError) as exc:
                    last_error = exc
                    self._close_socket()
                else:
                    return
            if attempt < cfg.connect_attempts:
                time.sleep(cfg.connect_retry_delay_s)
        raise MinicapError(
            f"cannot connect to minicap at 127.0.0.1:{cfg.local_port} "
            f"after {cfg.connect_attempts} attempts: {last_error}"
        )

    def _read_banner(self) -> None:
        data = self._recv_exact(BANNER_SIZE)
        banner = MinicapBanner.parse(data)
        if banner.banner_size > BANNER_SIZE:
            # Future minicap versions may extend the banner; skip the tail.
            self._recv_exact(banner.banner_size - BANNER_SIZE)
        self._banner = banner
        ew, eh = self.config.effective_virtual_size
        if (banner.virtual_width, banner.virtual_height) != (ew, eh):
            log.warning(
                "minicap.virtual_size_mismatch",
                expected=(ew, eh),
                banner=(banner.virtual_width, banner.virtual_height),
                hint="vision calibration assumes 1080x2400 -- check rotation/projection",
            )
        log.info(
            "minicap.banner",
            pid=banner.pid,
            real=(banner.real_width, banner.real_height),
            virtual=(banner.virtual_width, banner.virtual_height),
            orientation=banner.orientation,
            quirks=banner.quirks,
        )

    def _read_frame(self) -> bytes:
        header = self._recv_exact(4)
        (length,) = struct.unpack("<I", header)
        if not (0 < length <= _MAX_FRAME_BYTES):
            raise MinicapError(
                f"implausible frame length {length} -- stream framing lost"
            )
        return self._recv_exact(length)

    def _drain_to_latest(self, frame: bytes) -> bytes:
        """Swallow frames already buffered in the socket; return the newest.

        If the stream dies DURING the drain we already hold a complete frame:
        return it and reconnect lazily on the next :meth:`capture`.
        """
        for _ in range(_MAX_DRAIN_FRAMES):
            sock = self._sock
            if sock is None:
                return frame
            readable, _, _ = select.select([sock], [], [], 0.0)
            if not readable:
                return frame
            try:
                frame = self._read_frame()
            except (OSError, MinicapConnectionError) as exc:
                self._close_socket()
                log.warning("minicap.stream_dropped_after_frame", error=str(exc))
                return frame
        return frame

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly ``n`` bytes, looping over partial ``recv`` returns."""
        sock = self._sock
        if sock is None:
            raise MinicapConnectionError("socket is not connected")
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except TimeoutError as exc:
                raise MinicapConnectionError(
                    f"stream read timed out after {self.config.read_timeout_s}s "
                    f"({len(buf)}/{n} bytes)"
                ) from exc
            if not chunk:
                raise MinicapConnectionError(
                    f"connection closed mid-read ({len(buf)}/{n} bytes)"
                )
            buf += chunk
        return bytes(buf)

    def _decode_frame(self, data: bytes) -> Image:
        """JPEG bytes -> RGB uint8 ndarray (Pillow, same lib as adb.py's PNG path)."""
        pil_image = _import_pil_image()
        try:
            with pil_image.open(io.BytesIO(data)) as img:
                arr = cast(
                    npt.NDArray[np.uint8],
                    np.asarray(img.convert("RGB"), dtype=np.uint8),
                )
        except Exception as exc:
            raise MinicapError(
                f"cannot decode {len(data)}-byte JPEG frame: {exc}"
            ) from exc
        if arr.ndim != 3 or arr.shape[2] != 3:
            raise MinicapError(f"decoded frame has shape {arr.shape}, expected (H, W, 3)")
        return arr

    def _close_socket(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

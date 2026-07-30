"""Frame acquisition seam for the live loop: ``FrameSource`` + legacy screencap.

Vision and runner code should depend on :class:`FrameSource` only, never on a
concrete capture mechanism. Every implementation returns the exact array type
``bot2048.vision`` expects: RGB ``uint8`` ndarray of shape ``(H, W, 3)``.

Implementations:

* :class:`bot2048.device.scrcpy.ScrcpySource` -- PRIMARY on this device.
  Streams raw H.264 from scrcpy-server and decodes it through a host
  ``ffmpeg`` subprocess; a fresh frame is continuously available instead of
  a ~0.65 s ``screencap`` round-trip per move (docs/live-setup.md).
* :class:`bot2048.device.minicap.MinicapSource` -- DEAD on this device
  (Android 13 / SDK 33: no compatible minicap.so exists, see
  docs/live-setup.md). Kept working + unit-tested for older devices.
* :class:`ScreencapSource` (here) -- DEPRECATED for normal capture, but kept
  fully working: it is the fallback when no streaming source is available
  AND the canonical AUTH_REQUIRED sensor. Under a ``FLAG_SECURE`` window
  (the monobank auth screen) ``screencap -p`` returns 0 bytes -- a hard,
  reliable signal -- while the streaming sources silently produce BLACK
  frames (no error), so the auth probe MUST stay on the screencap path
  (see :meth:`ScreencapSource.is_auth_blocked`).

:func:`make_frame_source` is the small factory seam callers use to pick a
streaming source by name without importing concrete modules.

:class:`StreamingCaptureDevice` is the live-loop adapter on top of all of the
above: it looks like an ``AdbDevice`` (satisfies the runner's ``LiveDevice``
and the supervisor's ``SupervisorDevice`` protocols) but serves
``screenshot()`` frames from a streaming :class:`FrameSource`, with an
automatic, never-crashing fallback to the wrapped device's ``screencap``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

import numpy as np
import numpy.typing as npt
import structlog

from bot2048.core.types import Direction
from bot2048.device.adb import EmptyScreencapError
from bot2048.device.config import DEFAULT_SCREEN_SIZE
from bot2048.device.humanizer import BBox, Humanizer, SwipeParams

log = structlog.get_logger(__name__)

__all__ = [
    "CaptureDevice",
    "FrameSource",
    "Image",
    "MinicapConfig",
    "ScrcpyConfig",
    "ScreencapDevice",
    "ScreencapSource",
    "StreamingCaptureDevice",
    "make_frame_source",
]

#: The pixel contract of the whole pipeline: RGB uint8, shape (H, W, 3).
Image = npt.NDArray[np.uint8]

#: Allowed minicap display rotations (degrees).
_VALID_ROTATIONS: frozenset[int] = frozenset({0, 90, 180, 270})


@runtime_checkable
class FrameSource(Protocol):
    """A start/capture/close pixel source -- the live loop's only capture seam."""

    def start(self) -> None:
        """Acquire resources (device process, forward, socket). Idempotent."""

    def capture(self) -> Image:
        """Return the freshest full frame as RGB uint8 ``(H, W, 3)``."""

    def close(self) -> None:
        """Release everything. Safe to call more than once."""

    def __enter__(self) -> FrameSource: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MinicapConfig:
    """Knobs for :class:`bot2048.device.minicap.MinicapSource`.

    Lives here (not in :class:`bot2048.device.config.DeviceConfig`) so the
    device config stays untouched while minicap is being brought up; if a
    ``LiveConfig`` ever appears, these fields (``device_serial``,
    ``local_port``, ``virtual_size``, ``bin_dir``) are the ones to fold in.

    Attributes:
        device_serial: adb serial (e.g. ``192.0.2.10:5555``); ``None`` ->
            the single connected device.
        local_port: host TCP port for ``adb forward tcp:<port>
            localabstract:minicap``.
        virtual_size: minicap output (width, height). ``None`` -> **1:1 with
            ``real_size``** (no downscale), which keeps the existing vision
            calibration (done at 1080x2400) valid. Set explicitly only when
            deliberately downscaling -- vision must be recalibrated then.
        bin_dir: on-device directory holding ``minicap`` + ``minicap.so``.
        real_size: physical screen (width, height); the ``-P`` projection's
            left half.
        rotation: display rotation for the projection (0/90/180/270).
        connect_timeout_s: TCP connect timeout per attempt.
        connect_attempts: socket connect attempts (minicap needs a moment to
            open its abstract socket after the process starts).
        connect_retry_delay_s: pause between connect attempts.
        read_timeout_s: max wait for stream bytes. On a fully static screen
            minicap may emit nothing; a timeout then triggers a reconnect,
            and minicap always pushes the current frame to a fresh client,
            so ``capture()`` still returns.
        reconnect_attempts: extra capture attempts after a stream drop
            (each re-runs the full connect + banner handshake).
    """

    device_serial: str | None = None
    local_port: int = 1313
    virtual_size: tuple[int, int] | None = None
    bin_dir: str = "/data/local/tmp"
    real_size: tuple[int, int] = DEFAULT_SCREEN_SIZE
    rotation: int = 0
    connect_timeout_s: float = 3.0
    connect_attempts: int = 10
    connect_retry_delay_s: float = 0.25
    read_timeout_s: float = 2.0
    reconnect_attempts: int = 2

    def __post_init__(self) -> None:
        if not (0 < self.local_port < 65536):
            raise ValueError(f"invalid local_port {self.local_port}")
        for name, size in (("real_size", self.real_size), ("virtual_size", self.virtual_size)):
            if size is not None and (size[0] <= 0 or size[1] <= 0):
                raise ValueError(f"invalid {name} {size!r}")
        if self.rotation not in _VALID_ROTATIONS:
            raise ValueError(
                f"rotation must be one of {sorted(_VALID_ROTATIONS)}, got {self.rotation}"
            )
        if self.connect_attempts < 1:
            raise ValueError(f"connect_attempts must be >= 1, got {self.connect_attempts}")
        if self.reconnect_attempts < 0:
            raise ValueError(f"reconnect_attempts must be >= 0, got {self.reconnect_attempts}")
        if self.connect_timeout_s <= 0 or self.read_timeout_s <= 0:
            raise ValueError("connect_timeout_s and read_timeout_s must be > 0")
        if self.connect_retry_delay_s < 0:
            raise ValueError("connect_retry_delay_s must be >= 0")

    @property
    def effective_virtual_size(self) -> tuple[int, int]:
        """``virtual_size`` or -- the default -- ``real_size`` (1:1, no downscale)."""
        return self.virtual_size if self.virtual_size is not None else self.real_size


#: scrcpy scids are parsed on-device as a signed 31-bit hex int.
_SCID_MAX: int = 0x7FFFFFFF


@dataclass(frozen=True, slots=True)
class ScrcpyConfig:
    """Knobs for :class:`bot2048.device.scrcpy.ScrcpySource`.

    Lives next to :class:`MinicapConfig` for the same reason: the device
    config stays untouched; fold into a future ``LiveConfig`` if one appears.

    Attributes:
        device_serial: adb serial; ``None`` -> the single connected device.
        local_port: host TCP port for ``adb forward tcp:<port>
            localabstract:scrcpy_<scid>``. Distinct from minicap's 1313.
        scid: scrcpy session id -- exactly 8 hex chars, value <= 7fffffff.
            Also names the abstract socket (``scrcpy_<scid>``).
        server_path: on-device scrcpy-server jar (already pushed by the
            bring-up agent; ScrcpySource installs nothing).
        server_version: version argument the jar expects (must match the
            pushed jar or the server exits immediately).
        ffmpeg_path: host ffmpeg binary; default resolves via PATH.
        size: the FULL screen (width, height). With ``max_size=0`` the
            stream is native (1080x2400 here) unless ``crop`` narrows it;
            the decoded geometry (see :attr:`frame_size`) is cross-checked
            against ffmpeg's stderr and a mismatch kills the pipeline
            (frame framing would be garbage otherwise).
        crop: optional server-side crop ``(x, y, width, height)`` in
            screen-space pixels (device natural orientation, applied before
            scaling) -- becomes the scrcpy-server ``crop=W:H:X:Y`` argument
            and shrinks every decoded frame to ``(width, height)``. Use
            :mod:`bot2048.device.crop` to derive a board-only region and the
            matching crop-space calibration; frames from a cropped source
            MUST NOT feed full-frame vision (``detect_state`` anchors live
            outside the board). ``None`` (default): full frames, behavior
            unchanged.
        video_bit_rate: encoder bitrate (bits/s).
        max_fps: encoder frame-rate cap.
        connect_timeout_s: TCP connect + first-byte-probe timeout per attempt.
        connect_attempts: socket connect attempts (the server needs a moment
            to open its abstract socket after ``app_process`` starts).
        connect_retry_delay_s: pause between connect attempts.
        first_frame_timeout_s: max wait in ``capture()`` for a decoded frame
            on a fresh pipeline (server boot + keyframe + decode).
        reconnect_attempts: extra capture attempts after a pipeline death
            (each relaunches the server and rebuilds socket + ffmpeg).
        black_frame_limit: consecutive all-black decoded frames that trip a
            :class:`~bot2048.device.scrcpy.ScrcpySecureError` -- the signal
            that a ``FLAG_SECURE`` window (the monobank auth wall) is on top,
            where scrcpy silently streams BLACK instead of erroring, so the
            runner must fall back to the ``screencap`` 0-byte AUTH sensor.
            ``0`` disables the check. The 2048 board and monobank home are
            never all-black, so a sustained black run is an unambiguous
            secure/blanked screen; a non-black frame resets the counter, so a
            lone transient black frame cannot trip it.
        black_pixel_max: a decoded frame counts as "black" when its maximum
            channel value is ``<=`` this. Not 0: H.264 limited-range YUV->RGB
            rounding leaves true black a few counts above 0.
    """

    device_serial: str | None = None
    local_port: int = 27183
    scid: str = "01234567"
    server_path: str = "/data/local/tmp/scrcpy-server"
    server_version: str = "4.0"
    ffmpeg_path: str = "ffmpeg"
    size: tuple[int, int] = DEFAULT_SCREEN_SIZE
    crop: tuple[int, int, int, int] | None = None
    video_bit_rate: int = 8_000_000
    max_fps: int = 60
    connect_timeout_s: float = 3.0
    connect_attempts: int = 10
    connect_retry_delay_s: float = 0.25
    first_frame_timeout_s: float = 10.0
    reconnect_attempts: int = 2
    black_frame_limit: int = 30
    black_pixel_max: int = 8

    def __post_init__(self) -> None:
        if not (0 < self.local_port < 65536):
            raise ValueError(f"invalid local_port {self.local_port}")
        if len(self.scid) != 8:
            raise ValueError(f"scid must be exactly 8 hex chars, got {self.scid!r}")
        try:
            value = int(self.scid, 16)
        except ValueError:
            raise ValueError(f"scid must be hex, got {self.scid!r}") from None
        if value > _SCID_MAX:
            raise ValueError(f"scid must be <= {_SCID_MAX:08x}, got {self.scid!r}")
        if self.size[0] <= 0 or self.size[1] <= 0:
            raise ValueError(f"invalid size {self.size!r}")
        if self.crop is not None:
            x, y, width, height = self.crop
            if x < 0 or y < 0 or width <= 0 or height <= 0:
                raise ValueError(f"invalid crop {self.crop!r} (x, y >= 0; width, height > 0)")
            if width % 2 or height % 2:
                raise ValueError(f"crop size must be even (H.264 4:2:0), got {width}x{height}")
            if x + width > self.size[0] or y + height > self.size[1]:
                raise ValueError(
                    f"crop {self.crop!r} exceeds the {self.size[0]}x{self.size[1]} screen"
                )
        if self.video_bit_rate <= 0 or self.max_fps <= 0:
            raise ValueError("video_bit_rate and max_fps must be > 0")
        if self.connect_attempts < 1:
            raise ValueError(f"connect_attempts must be >= 1, got {self.connect_attempts}")
        if self.reconnect_attempts < 0:
            raise ValueError(f"reconnect_attempts must be >= 0, got {self.reconnect_attempts}")
        if self.connect_timeout_s <= 0 or self.first_frame_timeout_s <= 0:
            raise ValueError("connect_timeout_s and first_frame_timeout_s must be > 0")
        if self.connect_retry_delay_s < 0:
            raise ValueError("connect_retry_delay_s must be >= 0")
        if self.black_frame_limit < 0:
            raise ValueError(f"black_frame_limit must be >= 0, got {self.black_frame_limit}")
        if not (0 <= self.black_pixel_max <= 255):
            raise ValueError(f"black_pixel_max must be in [0, 255], got {self.black_pixel_max}")

    @property
    def socket_name(self) -> str:
        """The on-device abstract socket the server listens on."""
        return f"scrcpy_{self.scid}"

    @property
    def frame_size(self) -> tuple[int, int]:
        """Decoded frame (width, height): the ``crop`` size when set, else ``size``."""
        if self.crop is not None:
            return (self.crop[2], self.crop[3])
        return self.size

    @property
    def frame_bytes(self) -> int:
        """Bytes per decoded rawvideo rgb24 frame (W*H*3 of :attr:`frame_size`)."""
        width, height = self.frame_size
        return width * height * 3


class ScreencapDevice(Protocol):
    """The only capability :class:`ScreencapSource` needs (AdbDevice satisfies it)."""

    def screenshot(self) -> Image: ...


_SCREENCAP_DEPRECATION_WARNED: bool = False


class ScreencapSource:
    """DEPRECATED: use MinicapSource for normal capture.

    Per-call ``screencap -p`` wrapper around
    :meth:`bot2048.device.adb.AdbDevice.screenshot`. Kept fully working for
    two reasons:

    * fallback capture path when minicap is not installed / misbehaves;
    * **the canonical AUTH_REQUIRED sensor.** Under a ``FLAG_SECURE`` window
      (the monobank auth screen, «Уведіть ПІН-код») ``screencap -p`` returns
      **0 bytes**, surfaced as :class:`~bot2048.device.adb.EmptyScreencapError`
      once every retry came back empty. minicap does NOT give this signal:
      on secure screens it keeps streaming plain BLACK frames (not an
      error), indistinguishable from a genuinely dark app screen. The
      0-byte screencap probe therefore stays authoritative -- see
      :meth:`is_auth_blocked`.

    ``start()``/``close()`` are no-ops: the underlying
    :class:`~bot2048.device.adb.AdbDevice` connection is owned by the caller
    (the supervisor/runner share the same device object).
    """

    def __init__(self, device: ScreencapDevice) -> None:
        global _SCREENCAP_DEPRECATION_WARNED
        if not _SCREENCAP_DEPRECATION_WARNED:
            _SCREENCAP_DEPRECATION_WARNED = True
            log.warning(
                "capture.screencap_source_deprecated",
                hint="use bot2048.device.minicap.MinicapSource for normal capture; "
                "ScreencapSource remains the fallback and the AUTH_REQUIRED sensor",
            )
        self._device = device

    def start(self) -> None:
        """No-op -- the caller owns the AdbDevice connection."""

    def capture(self) -> Image:
        """One fresh ``screencap -p`` frame, RGB uint8 ``(H, W, 3)``.

        Raises whatever :meth:`AdbDevice.screenshot` raises, including
        :class:`~bot2048.device.adb.EmptyScreencapError` on the auth wall.
        """
        return self._device.screenshot()

    def close(self) -> None:
        """No-op -- the caller owns the AdbDevice connection."""

    def is_auth_blocked(self) -> bool:
        """The 0-byte AUTH probe: ``True`` when screencap is stably empty.

        ``True`` == ``screencap -p`` returned 0 bytes on every retry, i.e. a
        ``FLAG_SECURE`` window (the auth screen) is on top. Any other
        failure (device lost, undecodable frames) propagates as
        :class:`~bot2048.device.adb.DeviceError` -- it is NOT an auth signal
        and must go through the supervisor's classification instead.
        """
        try:
            self._device.screenshot()
        except EmptyScreencapError:
            return True
        return False

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


class CaptureDevice(Protocol):
    """Device surface :class:`StreamingCaptureDevice` wraps and delegates to.

    :class:`~bot2048.device.adb.AdbDevice` satisfies it. Mirrors the runner's
    ``LiveDevice`` protocol (superset of the supervisor's
    ``SupervisorDevice``) so the wrapper is a drop-in replacement everywhere
    a device object is expected.
    """

    def screenshot(self) -> Image: ...

    def ui_dump(self) -> str: ...

    def tap(self, x: int, y: int, *, sensitive: bool = False) -> None: ...

    def wake(self) -> None: ...

    def swipe_direction(
        self, direction: Direction, grid_bbox: BBox, humanizer: Humanizer
    ) -> SwipeParams: ...


class StreamingCaptureDevice:
    """A ``LiveDevice`` whose ``screenshot()`` prefers a streaming source.

    Wraps a real :class:`CaptureDevice` (``AdbDevice``) and a streaming
    :class:`FrameSource` (scrcpy). Everything except ``screenshot()`` --
    ``ui_dump``/``tap``/``wake``/``swipe_direction`` -- delegates verbatim to
    the wrapped device. ``screenshot()`` implements the fallback ladder:

    1. **Disabled** (``max_failures`` consecutive stream failures seen):
       plain ``device.screenshot()`` forever -- the stream is never touched
       again for this process.
    2. **Secure hold** (the source raised
       :class:`~bot2048.device.scrcpy.ScrcpySecureError`, i.e. a
       ``FLAG_SECURE`` auth wall is suspected): every capture goes through
       ``device.screenshot()`` -- the canonical 0-byte AUTH sensor -- and its
       :class:`~bot2048.device.adb.EmptyScreencapError` PROPAGATES so
       ``probe_screen`` classifies AUTH_REQUIRED exactly as before. The
       first *successful* screencap clears the hold (wall gone) and the next
       ``screenshot()`` restarts the stream.
    3. **Streaming**: lazily ``close()`` + ``start()`` the source on first
       use (or after any failure), then ``capture()``.

       * All-black frame (max channel ``<= black_pixel_max``): serve one
         ``device.screenshot()`` instead -- an auth wall streams BLACK
         silently, so black frames must not feed vision -- but keep the
         source alive (its own ``black_frame_limit`` escalates a sustained
         run to ``ScrcpySecureError``). Not counted as a failure.
       * ``ScrcpySecureError``: close the source, enter the secure hold,
         serve ``device.screenshot()``.
       * Any other source exception: close the source (best-effort), count
         one failure, serve ``device.screenshot()``. ``max_failures``
         consecutive failures -> ladder step 1 (disabled). A successful
         stream frame resets the counter.

    Device exceptions are NEVER swallowed: whatever ``device.screenshot()``
    raises propagates, preserving the supervisor's classification contract.

    Ops note: the scrcpy-server jar must already be on the device
    (``bot2048 live play`` best-effort pushes
    ``tools/scrcpy/scrcpy-win64-v4.0/scrcpy-server`` to
    ``/data/local/tmp/scrcpy-server`` before starting); a missing jar simply
    lands in the generic-failure branch and screencap keeps the run alive.
    """

    def __init__(
        self,
        device: CaptureDevice,
        source: FrameSource,
        *,
        max_failures: int = 3,
        black_pixel_max: int = 8,
    ) -> None:
        if max_failures < 1:
            raise ValueError(f"max_failures must be >= 1, got {max_failures}")
        if not (0 <= black_pixel_max <= 255):
            raise ValueError(f"black_pixel_max must be in [0, 255], got {black_pixel_max}")
        self._device = device
        self._source = source
        self._max_failures = max_failures
        self._black_pixel_max = black_pixel_max
        self._failures = 0
        self._disabled = False
        self._secure_hold = False
        self._source_started = False
        self._black_streak = False

    # -- capture path ------------------------------------------------------

    def screenshot(self) -> Image:
        """One fresh RGB uint8 ``(H, W, 3)`` frame via the fallback ladder."""
        if self._disabled:
            return self._device.screenshot()
        if self._secure_hold:
            # EmptyScreencapError/DeviceError propagate: the AUTH sensor.
            frame = self._device.screenshot()
            # Screencap succeeded => the FLAG_SECURE wall is gone; re-probe
            # the stream with a fresh start() on the NEXT capture.
            self._secure_hold = False
            self._source_started = False
            return frame
        try:
            if not self._source_started:
                # close() first: clears sticky secure/pipeline state so
                # start() builds a genuinely fresh pipeline (idempotent and
                # a no-op on a never-started source).
                self._source.close()
                self._source.start()
                self._source_started = True
            frame = self._source.capture()
        except Exception as exc:
            return self._fallback(exc)
        if frame.size and int(frame.max()) <= self._black_pixel_max:
            if not self._black_streak:
                self._black_streak = True
                log.warning(
                    "capture.streaming_black_frame",
                    hint="stream is black (FLAG_SECURE?); serving screencap for this frame",
                )
            return self._device.screenshot()
        self._black_streak = False
        self._failures = 0
        return frame

    def _fallback(self, error: Exception) -> Image:
        """Handle a stream failure: log, book-keep, serve a screencap frame."""
        from bot2048.device.scrcpy import ScrcpySecureError  # lazy: no import cycle

        self._close_source()
        self._source_started = False
        if isinstance(error, ScrcpySecureError):
            self._secure_hold = True
            log.warning(
                "capture.streaming_secure_fallback",
                error=str(error),
                hint="FLAG_SECURE suspected; holding on screencap until it succeeds",
            )
            return self._device.screenshot()
        self._failures += 1
        if self._failures >= self._max_failures:
            self._disabled = True
            log.warning(
                "capture.streaming_disabled",
                error=str(error),
                failures=self._failures,
                hint="streaming capture disabled for this run; screencap from now on",
            )
        else:
            log.warning(
                "capture.streaming_fallback",
                error=str(error),
                failures=self._failures,
                max_failures=self._max_failures,
            )
        return self._device.screenshot()

    def _close_source(self) -> None:
        """Best-effort ``source.close()`` -- a broken close must not crash play."""
        try:
            self._source.close()
        except Exception as exc:
            log.warning("capture.streaming_close_failed", error=str(exc))

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Eager warm-up (optional; ``screenshot()`` also starts lazily).

        Never raises: a failed warm-up is logged and counted via the same
        failure accounting as a failed capture.
        """
        if self._disabled or self._source_started:
            return
        try:
            self._source.start()
            self._source_started = True
        except Exception as exc:
            self._failures += 1
            if self._failures >= self._max_failures:
                self._disabled = True
            log.warning(
                "capture.streaming_start_failed",
                error=str(exc),
                failures=self._failures,
                disabled=self._disabled,
            )

    def close(self) -> None:
        """Release the streaming source. Never raises; safe to call twice."""
        self._close_source()
        self._source_started = False

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

    # -- verbatim delegation ------------------------------------------------

    def ui_dump(self) -> str:
        return self._device.ui_dump()

    def tap(self, x: int, y: int, *, sensitive: bool = False) -> None:
        self._device.tap(x, y, sensitive=sensitive)

    def wake(self) -> None:
        self._device.wake()

    def swipe_direction(
        self, direction: Direction, grid_bbox: BBox, humanizer: Humanizer
    ) -> SwipeParams:
        return self._device.swipe_direction(direction, grid_bbox, humanizer)


def make_frame_source(
    kind: str = "scrcpy",
    *,
    device_serial: str | None = None,
    adb_exe: str | None = None,
    ffmpeg_path: str | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> FrameSource:
    """Pick a streaming :class:`FrameSource` by name (lazy imports, no cycles).

    Kinds:

    * ``"scrcpy"`` -- :class:`~bot2048.device.scrcpy.ScrcpySource`, the fast
      path on this device (raw H.264 + host ffmpeg).
    * ``"minicap"`` -- :class:`~bot2048.device.minicap.MinicapSource`; dead
      on Android 13/SDK 33 but kept for older devices.

    ``adb_exe`` / ``ffmpeg_path`` apply to ``"scrcpy"`` only (the repo
    bundles both binaries under ``tools/``; the CLI resolves them): they are
    threaded into :class:`~bot2048.device.scrcpy.AdbScrcpyRunner` and
    :class:`ScrcpyConfig` respectively; ``None`` keeps the defaults (PATH
    lookup). ``"minicap"`` ignores them.

    ``crop`` (``"scrcpy"`` only, default ``None`` = full frames) is the
    server-side board crop ``(x, y, width, height)`` -- see
    :attr:`ScrcpyConfig.crop`. It is an EXPLICIT opt-in: callers must pair
    the cropped stream with :func:`bot2048.device.crop.crop_calibration` and
    keep state detection on full frames (the fail-closed contract in
    :mod:`bot2048.device.crop`). The ``BOT2048_SCRCPY_CROP`` env flag is
    deliberately NOT consumed here -- resolve it via
    :func:`bot2048.device.crop.board_crop_from_env` at a call site that
    controls the calibration, so the default live path cannot silently
    receive frames its vision was not calibrated for.

    ``ScreencapSource`` is deliberately NOT constructible here: it wraps a
    caller-owned :class:`~bot2048.device.adb.AdbDevice` (the AUTH sensor),
    so callers build it directly around their existing device object.
    """
    if kind == "scrcpy":
        from bot2048.device.scrcpy import AdbScrcpyRunner, ScrcpySource

        config = (
            ScrcpyConfig(device_serial=device_serial, crop=crop)
            if ffmpeg_path is None
            else ScrcpyConfig(device_serial=device_serial, ffmpeg_path=ffmpeg_path, crop=crop)
        )
        runner = (
            None if adb_exe is None else AdbScrcpyRunner(adb_exe=adb_exe, serial=device_serial)
        )
        return ScrcpySource(config, runner=runner)
    if kind == "minicap":
        from bot2048.device.minicap import MinicapSource

        return MinicapSource(MinicapConfig(device_serial=device_serial))
    raise ValueError(f"unknown frame source kind {kind!r} (expected 'scrcpy' or 'minicap')")

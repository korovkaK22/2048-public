"""``--capture`` kind -> capture device / on-device board source for ``live play``."""

from __future__ import annotations

import typer

from bot2048.device.adb import AdbDevice
from bot2048.device.apk_capture import ApkBoardSource
from bot2048.device.capture import StreamingCaptureDevice, make_frame_source
from bot2048.runner.cli.common import err_console
from bot2048.runner.cli.tools import _resolve_adb_exe, _resolve_ffmpeg_exe

_CAPTURE_KINDS = ("scrcpy", "screencap", "apk")


def _make_board_source(kind: str, serial: str | None) -> ApkBoardSource | None:
    """The optional fast on-device board source for the loop.

    Only ``"apk"`` has one: an :class:`~bot2048.device.apk_capture.ApkBoardSource`
    that reads 22-byte board packets from the sampler APK over ``adb forward``
    (option D, docs/ondevice-vision-design.md). ``"scrcpy"`` / ``"screencap"``
    have no board source (``None``) -- they read the board from full frames as
    before. Construction is side-effect-free: the forward + socket are set up
    lazily on the first poll (or an explicit ``start()``).
    """
    if kind != "apk":
        return None
    return ApkBoardSource(serial=serial, adb_exe=_resolve_adb_exe())


def _make_capture_device(
    device: AdbDevice, kind: str, serial: str | None
) -> tuple[AdbDevice | StreamingCaptureDevice, StreamingCaptureDevice | None]:
    """Build the ``play`` capture path: ``(device_for_the_loop, owner_to_close)``.

    * ``"screencap"`` -- the plain :class:`AdbDevice` (identity; nothing to
      close beyond what the caller already owns).
    * ``"apk"`` -- also the plain :class:`AdbDevice`: the per-move BOARD comes
      from the sampler APK (see :func:`_make_board_source`), while full frames
      (``detect_state``, the auth 0-byte oracle, initial/settle reads, the fast
      path's fallback) must stay on ``screencap`` -- the streaming sources go
      silently black under ``FLAG_SECURE``, so the auth wall would be invisible.
    * ``"scrcpy"`` -- :class:`~bot2048.device.capture.StreamingCaptureDevice`
      around a :class:`~bot2048.device.scrcpy.ScrcpySource` (fast H.264
      stream with automatic screencap fallback). Construction is
      side-effect-free: nothing touches adb or ffmpeg until the first
      capture (or an explicit ``start()``).

    The second tuple element is the wrapper to ``close()`` after the run
    (``None`` for the identity path).
    """
    if kind in ("screencap", "apk"):
        return device, None
    if kind == "scrcpy":
        source = make_frame_source(
            "scrcpy",
            device_serial=serial,
            adb_exe=_resolve_adb_exe(),
            ffmpeg_path=_resolve_ffmpeg_exe(),
        )
        wrapped = StreamingCaptureDevice(device, source)
        return wrapped, wrapped
    err_console.print(
        f"[red]unknown capture source:[/red] {kind!r} "
        f"(expected one of {', '.join(_CAPTURE_KINDS)})"
    )
    raise typer.Exit(code=1)

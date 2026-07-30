"""Zero-spawn swipe delivery via the on-device sampler APK's accessibility service.

The fast board path already runs a helper APK (``ai.bot2048.sampler``) that
streams board packets over a persistent ``adb forward`` socket
(:mod:`bot2048.device.apk_capture`). This module reuses that SAME socket to
inject the per-move swipe **on the phone**: the host writes a
:data:`~bot2048.ondevice.packet.CMD_SWIPE` command, and the APK's
:class:`AccessibilityService` replays it with ``dispatchGesture()``.

Why this exists (the latency win): the previous fast transport,
:class:`bot2048.device.swipe_shell.PersistentSwipeShell`, already removed the
per-swipe ``adb`` process fork by keeping one ``adb shell`` open -- but every
swipe still forks a fresh Dalvik ``input`` process ON THE DEVICE (~60 ms) and
crosses the adb-shell hop. Dispatching the gesture from a resident service
removes BOTH: no ``input`` spawn, no shell round-trip. The command is ~11 bytes
on a socket the phone is already draining, so the host cost is a single
``sendall``.

Design (mirrors :class:`~bot2048.device.swipe_shell.PersistentSwipeShell`'s
"never drop a move" contract):

* **Capability probe, once.** On the first swipe the injector checks whether the
  accessibility service is actually enabled (``adb shell settings get secure
  enabled_accessibility_services``). If it is not -- the operator never did the
  one-time Settings -> Accessibility enable -- injection is disabled for the
  session and every swipe uses the ``input swipe`` fallback. This is the only
  adb spawn this module ever makes, and it happens once.
* **Fire-and-forget + local timing.** The command is fire-and-forget (no ack on
  the board read stream, which must stay a clean packet demux). The gesture
  takes ``duration_ms`` on the device, so the injector blocks for that span to
  reproduce the old ``input swipe`` blocking semantics; the runner's
  board-settle gate then confirms the move landed exactly as before.
* **Fallback, always.** A socket write that fails (link dropped mid-session)
  falls back to ``input swipe`` for that move and keeps injection armed for the
  next one. Injection is only permanently disabled when the one-time probe says
  the service is not enabled. The loop is never crashed by an injection failure.

This backend is a drop-in alternate to the ``input``-shell swipe: it exposes
:meth:`ApkSwipeInjector.swipe`, taking the same
:class:`~bot2048.device.humanizer.SwipeParams` the runner already computes and
:meth:`bot2048.device.adb.AdbDevice.swipe` already executes. Wiring point (a
future, separate change to keep this branch device-safe): in
:meth:`AdbDevice.swipe`, try ``self._apk_swipe.swipe(params)`` before the
persistent-shell path, passing ``self._swipe_oneshot`` (or the shell's swipe)
as the ``fallback``.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

import structlog

from bot2048.device.humanizer import SwipeParams

__all__ = [
    "ACCESSIBILITY_COMPONENT",
    "ACCESSIBILITY_SERVICE_SHORT",
    "ApkSwipeInjector",
    "SwipeSender",
    "accessibility_enabled",
]

log = structlog.get_logger(__name__)

#: The APK package that hosts both the board sampler and the swipe service.
_SAMPLER_PACKAGE: str = "ai.bot2048.sampler"

#: Short component name of the swipe accessibility service (as declared in the
#: APK manifest). ``settings`` stores enabled services in this ``pkg/.Class``
#: form (or the fully-qualified form); the probe accepts either.
ACCESSIBILITY_SERVICE_SHORT: str = f"{_SAMPLER_PACKAGE}/.SwipeAccessibilityService"

#: Fully-qualified component name (``pkg/pkg.Class``).
ACCESSIBILITY_COMPONENT: str = (
    f"{_SAMPLER_PACKAGE}/{_SAMPLER_PACKAGE}.SwipeAccessibilityService"
)

#: ``(x1, y1, x2, y2, duration_ms) -> bool`` -- write one CMD_SWIPE over the
#: live sampler socket; ``True`` on success, ``False`` when no link / write
#: failed (:meth:`bot2048.device.apk_capture.ApkBoardSource.send_swipe`).
SwipeSender = Callable[[int, int, int, int, int], bool]


def accessibility_enabled(
    *,
    adb_exe: str = "adb",
    serial: str | None = None,
    component_markers: tuple[str, ...] = (
        ACCESSIBILITY_SERVICE_SHORT,
        ACCESSIBILITY_COMPONENT,
    ),
    timeout_s: float = 5.0,
) -> bool:
    """Return whether the swipe accessibility service is enabled on the device.

    Reads ``settings get secure enabled_accessibility_services`` (a
    colon-separated list of enabled service components) and looks for the
    sampler's service. Any adb error is treated as "not enabled" (logged) so
    the caller safely falls back to ``input swipe``. Spawned directly (no shell)
    so git-bash path mangling never applies.
    """
    cmd = [adb_exe]
    if serial is not None:
        cmd += ["-s", serial]
    cmd += ["shell", "settings", "get", "secure", "enabled_accessibility_services"]
    try:
        result = subprocess.run(
            cmd, check=False, capture_output=True, timeout=timeout_s
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("apk_swipe.probe_failed", error=str(exc), command=cmd)
        return False
    if result.returncode != 0:
        log.warning(
            "apk_swipe.probe_failed",
            returncode=result.returncode,
            stderr=result.stderr.decode(errors="replace").strip(),
        )
        return False
    enabled = result.stdout.decode(errors="replace").strip()
    # `settings get` prints the literal string "null" when nothing is enabled.
    if not enabled or enabled == "null":
        return False
    return any(marker in enabled for marker in component_markers)


class ApkSwipeInjector:
    """Deliver swipes via on-device gesture injection, falling back to shell.

    Not thread-safe across concurrent swipes; the live loop issues swipes
    serially (same assumption as
    :class:`~bot2048.device.swipe_shell.PersistentSwipeShell`).
    """

    def __init__(
        self,
        send_swipe: SwipeSender,
        fallback: Callable[[SwipeParams], None],
        *,
        probe: Callable[[], bool] | None = None,
        block: bool = True,
    ) -> None:
        """Create an injector.

        Args:
            send_swipe: writes one CMD_SWIPE over the live sampler socket
                (typically
                :meth:`bot2048.device.apk_capture.ApkBoardSource.send_swipe`).
            fallback: the ``input swipe`` path to use whenever injection is
                unavailable or a write fails (e.g.
                :meth:`bot2048.device.adb.AdbDevice._swipe_oneshot` or the
                persistent-shell swipe). Must never be ``None`` -- a move is
                never dropped.
            probe: one-shot capability check; ``None`` -> assume the service is
                enabled and rely on write success only (a disabled service then
                silently drops the gesture, so pass a real probe such as
                :func:`accessibility_enabled` in production).
            block: sleep ``duration_ms`` after a successful injection so the
                call blocks for the gesture span, matching ``input swipe``.
        """
        self._send_swipe = send_swipe
        self._fallback = fallback
        self._probe = probe
        self._block = block
        #: ``None`` until the first swipe runs the one-time capability probe.
        self._available: bool | None = None

    def _ensure_probed(self) -> bool:
        """Run the one-time capability probe; cache and return availability."""
        if self._available is None:
            if self._probe is None:
                self._available = True
            else:
                self._available = self._probe()
                if self._available:
                    log.info("apk_swipe.injection_enabled")
                else:
                    log.warning(
                        "apk_swipe.injection_unavailable",
                        hint="accessibility service not enabled; using input-swipe "
                        "fallback (enable once: Settings -> Accessibility -> "
                        "bot2048 sampler)",
                    )
        return self._available

    def swipe(self, params: SwipeParams) -> None:
        """Deliver one swipe, injecting on-device when possible else falling back.

        Blocks until the gesture span elapses (``block=True``), preserving the
        board-settle timing the runner relies on. Never raises on an injection
        problem -- it degrades to the ``input swipe`` fallback instead.
        """
        if not self._ensure_probed():
            self._fallback(params)
            return
        sent = self._send_swipe(
            params.x1, params.y1, params.x2, params.y2, params.duration_ms
        )
        if not sent:
            # Link down mid-session: this move goes over the shell, but keep
            # injection armed -- the sampler link reconnects lazily.
            log.warning("apk_swipe.send_failed_fallback")
            self._fallback(params)
            return
        if self._block and params.duration_ms > 0:
            time.sleep(params.duration_ms / 1000.0)
        log.debug(
            "apk_swipe.injected",
            x1=params.x1,
            y1=params.y1,
            x2=params.x2,
            y2=params.y2,
            duration_ms=params.duration_ms,
        )

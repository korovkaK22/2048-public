"""Device layer configuration (plain data, no adb imports).

Facts baked in from real screenshots in ``screens/``:
the game lives inside the monobank app, package ``com.ftband.mono``,
reference device screen 1080x2400.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Android package that hosts the 2048 clone.
APP_PACKAGE: str = "com.ftband.mono"

#: Reference device screen (width, height) in pixels.
DEFAULT_SCREEN_SIZE: tuple[int, int] = (1080, 2400)

#: Hard floor for :attr:`DeviceConfig.adb_command_timeout_s`. The blocking adb
#: call MUST sit safely ABOVE the largest legitimate wait so a slow-but-honest
#: operation is never false-aborted: the live runner's endgame search budget is
#: up to ~9 s and the run may raise it toward ~15-20 s. A per-command adb bound
#: below ~15 s could clip a legitimate slow move and fire a spurious recovery,
#: which is as harmful as the hang it guards against -- so the floor is 15 s.
MIN_ADB_COMMAND_TIMEOUT_S: float = 15.0


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """Connection and device parameters for :class:`bot2048.device.adb.AdbDevice`.

    Button coordinates ("New game" bottom-center, popup buttons, close "x")
    are NOT here -- taps are only ever issued at coordinates measured during
    calibration (see docs/live-setup.md); exploratory taps are forbidden.

    Attributes:
        serial: adb device serial; ``None`` -> the single connected device.
        adb_host / adb_port: adb server address (defaults = local adb server).
        app_package: package to launch/foreground-check.
        screen_size: expected (width, height); mismatching screenshots are a
            hard error -- it means the wrong device or DPI changed.
        adb_command_timeout_s: real socket-read timeout threaded into EVERY
            blocking ``adb shell`` / input call (screencap, ui_dump, swipe,
            tap, keyevent, launch). Without it a half-open WiFi-ADB link makes
            those calls block FOREVER, turning a transient transport stall into
            a silent infinite hang (the heartbeat watchdog beats on a fixed
            interval and cannot see a loop hang). On expiry adbutils raises
            ``AdbTimeout``, which the device layer maps to
            :class:`~bot2048.device.adb.DeviceError` so the EXISTING
            ``DEVICE_LOST`` -> reconnect ladder recovers it. MUST stay at or
            above :data:`MIN_ADB_COMMAND_TIMEOUT_S` (see its note) so a legit
            slow endgame move is never false-aborted; default 20 s leaves
            headroom above both the 9 s budget and a ~15-20 s escalation.
        swipe_shell_timeout_s: watchdog bound (seconds) for one gesture on the
            persistent ``adb shell`` swipe channel. Its ``readline`` marker wait
            has no native timeout, so a wedged-but-open shell (device frozen
            mid-``input swipe``, WiFi stall) would block the live loop forever.
            On expiry the shell raises ``_ShellBroken`` so the EXISTING
            reconnect -> one-shot fallback re-establishes delivery. A swipe
            gesture is ~80-215 ms, so the 15 s floor is vast headroom and can
            never fire on a healthy-but-slow swipe. MUST stay at or above
            :data:`MIN_ADB_COMMAND_TIMEOUT_S`.
        screencap_retries: raw ``screencap -p`` attempts before giving up;
            0-byte output on EVERY attempt -> ``EmptyScreencapError``
            (FLAG_SECURE window, i.e. the auth screen).
        screencap_retry_delay_s: pause between screencap attempts.
        persistent_swipe_shell: keep ONE long-lived ``adb shell`` open and
            stream ``input swipe`` lines to it instead of spawning a fresh
            ``adb`` process per swipe (~83 ms saved/swipe). Default ``True``
            (the fast profile is the default); set ``False`` to force the
            legacy one-shot ``adb swipe`` path. A dead shell always falls back
            to one-shot, so disabling only trades speed, never reliability.
    """

    serial: str | None = None
    adb_host: str = "127.0.0.1"
    adb_port: int = 5037
    app_package: str = APP_PACKAGE
    screen_size: tuple[int, int] = DEFAULT_SCREEN_SIZE
    adb_command_timeout_s: float = 20.0
    swipe_shell_timeout_s: float = 15.0
    screencap_retries: int = 3
    screencap_retry_delay_s: float = 0.4
    persistent_swipe_shell: bool = True

    def __post_init__(self) -> None:
        w, h = self.screen_size
        if w <= 0 or h <= 0:
            raise ValueError(f"invalid screen_size {self.screen_size!r}")
        if not (0 < self.adb_port < 65536):
            raise ValueError(f"invalid adb_port {self.adb_port}")
        if self.adb_command_timeout_s < MIN_ADB_COMMAND_TIMEOUT_S:
            # A too-small bound is as dangerous as no bound: it would
            # false-abort a legitimate slow endgame move and fire a spurious
            # reconnect. Refuse it outright rather than let a misconfig ship.
            raise ValueError(
                f"adb_command_timeout_s must be >= {MIN_ADB_COMMAND_TIMEOUT_S} "
                f"(safely above the endgame search budget), got "
                f"{self.adb_command_timeout_s}"
            )
        if self.swipe_shell_timeout_s < MIN_ADB_COMMAND_TIMEOUT_S:
            raise ValueError(
                f"swipe_shell_timeout_s must be >= {MIN_ADB_COMMAND_TIMEOUT_S} "
                f"(vast headroom over a ~0.1 s swipe), got "
                f"{self.swipe_shell_timeout_s}"
            )
        if self.screencap_retries < 1:
            raise ValueError(f"screencap_retries must be >= 1, got {self.screencap_retries}")
        if self.screencap_retry_delay_s < 0:
            raise ValueError("screencap_retry_delay_s must be >= 0")

"""Device layer: ADB control of the phone + humanized input.

Modules:
    config    -- DeviceConfig, app package / screen constants (works today)
    humanizer -- randomized swipe geometry & timing, safe-zone clamped
                 (works today, unit-tested without hardware)
    adb       -- AdbDevice wrapper over adbutils (needs `[device]` extra + phone)
    capture   -- FrameSource protocol + DEPRECATED ScreencapSource (AUTH sensor)
                 + make_frame_source factory
    scrcpy    -- ScrcpySource, the fast streaming capture path on this device
                 (scrcpy-server raw H.264 + host ffmpeg)
    minicap   -- MinicapSource streaming path (DEAD on Android 13/SDK 33;
                 kept for older devices)
"""

from bot2048.device.adb import AdbDevice, DeviceError, MissingDeviceDependencyError
from bot2048.device.capture import (
    FrameSource,
    MinicapConfig,
    ScrcpyConfig,
    ScreencapSource,
    make_frame_source,
)
from bot2048.device.config import APP_PACKAGE, DeviceConfig
from bot2048.device.humanizer import (
    DEFAULT_SWIPE_DURATION_MS,
    PACING_ENV_VAR,
    SWIPE_MS_ENV_VAR,
    Humanizer,
    HumanizerConfig,
    PacingProfile,
    SwipeParams,
)
from bot2048.device.minicap import MinicapBanner, MinicapError, MinicapSource
from bot2048.device.scrcpy import ScrcpyError, ScrcpySecureError, ScrcpySource
from bot2048.device.swipe_shell import PersistentSwipeShell, SwipeShellError

__all__ = [
    "APP_PACKAGE",
    "DEFAULT_SWIPE_DURATION_MS",
    "PACING_ENV_VAR",
    "SWIPE_MS_ENV_VAR",
    "AdbDevice",
    "DeviceConfig",
    "DeviceError",
    "FrameSource",
    "Humanizer",
    "HumanizerConfig",
    "MinicapBanner",
    "MinicapConfig",
    "MinicapError",
    "MinicapSource",
    "MissingDeviceDependencyError",
    "PacingProfile",
    "PersistentSwipeShell",
    "ScrcpyConfig",
    "ScrcpyError",
    "ScrcpySecureError",
    "ScrcpySource",
    "ScreencapSource",
    "SwipeParams",
    "SwipeShellError",
    "make_frame_source",
]

"""Host-side binary / artifact resolution for the live CLI (adb, ffmpeg, scrcpy-server).

One code path per tool for every host OS -- no ``*_win`` / ``*_mac`` fork; each
resolver branches internally on env vars, filesystem layout and PATH. Only the
bundled ``adb`` / ``ffmpeg`` executable NAMES need the import-time
``sys.platform`` suffix switch (``.exe`` on Windows).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from bot2048.runner.cli.common import log

#: Repo-bundled binaries/artifacts (all gitignored), relative to project/.
#: ``adb`` and ``ffmpeg`` carry the OS-conditional ``.exe`` suffix; ffmpeg is
#: resolved PATH-first and only then searched for under ``tools/ffmpeg/`` (a
#: release zip unpacks into a versioned subdir -- ``ffmpeg-master-latest-
#: win64-gpl/bin/ffmpeg.exe`` on Windows, a different layout elsewhere -- so
#: the search is recursive, see :func:`_resolve_ffmpeg_exe`); the scrcpy-server
#: jar is resolved per-host by :func:`_resolve_scrcpy_server_jar`.
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
_BUNDLED_ADB = Path("tools") / "platform-tools" / f"adb{_EXE_SUFFIX}"
_FFMPEG_NAME = f"ffmpeg{_EXE_SUFFIX}"
_BUNDLED_FFMPEG_DIR = Path("tools") / "ffmpeg"

#: Where the (device-side, arch-neutral) scrcpy-server jar is looked for on the
#: host. The jar is pushed to the phone, so its *host* location differs only by
#: how the scrcpy release was unpacked/installed -- never by CPU arch.
#: Env overrides (checked first, in order): our own knob, then scrcpy's official
#: ``SCRCPY_SERVER_PATH``.
_SCRCPY_SERVER_ENV_VARS = ("BOT2048_SCRCPY_SERVER", "SCRCPY_SERVER_PATH")
#: Repo-bundled scrcpy releases live under here; the Windows zip unpacks the jar
#: at ``scrcpy-win64-v4.0/scrcpy-server`` while a macOS zip lands it elsewhere
#: under the same dir, so we search recursively rather than hardcode one layout.
_SCRCPY_TOOLS_DIR = Path("tools") / "scrcpy"
_SCRCPY_SERVER_DEVICE_PATH = "/data/local/tmp/scrcpy-server"


def _resolve_adb_exe() -> str:
    """Repo-local adb (``tools/platform-tools``, gitignored) if present, else PATH."""
    return str(_BUNDLED_ADB) if _BUNDLED_ADB.exists() else "adb"


def _resolve_ffmpeg_exe() -> str:
    """PATH ffmpeg if available, else a repo-bundled one, else plain ``ffmpeg``.

    PATH first on every OS (Homebrew/apt/winget installs). The bundled search
    is recursive under ``tools/ffmpeg/`` for the OS-specific executable name,
    newest mtime first, so no release-zip layout is hardcoded. The last case
    lets :class:`~bot2048.device.scrcpy.ScrcpySource` surface the spawn error
    itself -- the capture fallback then keeps the run alive on screencap.
    """
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    if _BUNDLED_FFMPEG_DIR.is_dir():
        bundled = sorted(
            (p for p in _BUNDLED_FFMPEG_DIR.rglob(_FFMPEG_NAME) if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if bundled:
            return str(bundled[0])
    return "ffmpeg"


def _resolve_scrcpy_server_jar() -> Path | None:
    """Locate the host copy of the (device-side, arch-neutral) scrcpy-server jar.

    One code path for every host OS -- no ``*_win`` / ``*_mac`` fork. Resolution
    order, first hit wins:

    1. ``BOT2048_SCRCPY_SERVER`` / ``SCRCPY_SERVER_PATH`` env override (an
       explicit path always wins).
    2. A ``scrcpy-server`` file bundled anywhere under ``tools/scrcpy/`` -- the
       Windows release unpacks it at ``scrcpy-win64-v4.0/scrcpy-server``; a
       macOS zip unpacked into the same dir lands it under a different subdir.
       Newest mtime first, so a fresh unpack wins over a stale sibling.
    3. Beside a ``scrcpy`` binary on PATH: Homebrew (macOS) installs the server
       at ``<prefix>/share/scrcpy/scrcpy-server`` (``bin/scrcpy`` ->
       ``../share/scrcpy``); some layouts keep it next to the binary.

    Returns ``None`` when nothing is found; the caller logs an actionable hint
    and lets capture fall back to screencap (this function never raises so the
    best-effort push contract holds).
    """
    for var in _SCRCPY_SERVER_ENV_VARS:
        raw = os.environ.get(var)
        if raw:
            candidate = Path(raw)
            if candidate.is_file():
                return candidate
    if _SCRCPY_TOOLS_DIR.is_dir():
        bundled = sorted(
            (p for p in _SCRCPY_TOOLS_DIR.rglob("scrcpy-server") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if bundled:
            return bundled[0]
    scrcpy_bin = shutil.which("scrcpy")
    if scrcpy_bin:
        bin_dir = Path(scrcpy_bin).resolve().parent
        for candidate in (
            bin_dir / "scrcpy-server",
            bin_dir.parent / "share" / "scrcpy" / "scrcpy-server",
        ):
            if candidate.is_file():
                return candidate
    return None


def _ensure_scrcpy_server_pushed(adb_exe: str, serial: str | None) -> None:
    """Best-effort idempotent push of the scrcpy-server jar (v4.0). NEVER raises.

    ``adb push`` re-pushing an identical file is harmless; a failed push is
    only logged -- :class:`~bot2048.device.capture.StreamingCaptureDevice`
    falls back to screencap when the server cannot start at capture time.
    ``adb`` is spawned directly (no shell), so git-bash's ``/data/...`` path
    mangling does not apply. The host jar location is resolved per-OS by
    :func:`_resolve_scrcpy_server_jar`.
    """
    server_jar = _resolve_scrcpy_server_jar()
    if server_jar is None:
        log.warning(
            "live.scrcpy_server_not_found",
            hint=(
                "no scrcpy-server jar found -- set BOT2048_SCRCPY_SERVER, unpack "
                "a scrcpy release under tools/scrcpy/, or install scrcpy on PATH "
                "(`brew install scrcpy` on macOS). Capture falls back to screencap."
            ),
            searched=str(_SCRCPY_TOOLS_DIR),
        )
        return
    cmd = [adb_exe]
    if serial is not None:
        cmd += ["-s", serial]
    cmd += ["push", str(server_jar), _SCRCPY_SERVER_DEVICE_PATH]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, timeout=30)
    except Exception as exc:
        log.warning("live.scrcpy_server_push_failed", error=str(exc), command=cmd)
        return
    if result.returncode != 0:
        log.warning(
            "live.scrcpy_server_push_failed",
            returncode=result.returncode,
            stderr=result.stderr.decode(errors="replace").strip(),
            command=cmd,
        )
    else:
        log.info("live.scrcpy_server_pushed", target=_SCRCPY_SERVER_DEVICE_PATH, serial=serial)

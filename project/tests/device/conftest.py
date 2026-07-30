"""Make the repo-bundled ffmpeg discoverable so the scrcpy decode tests run.

The ffmpeg-backed ``tests/device/test_scrcpy.py`` cases self-skip when
``shutil.which("ffmpeg")`` is ``None``. On this host ffmpeg is not on the
system PATH; a static build lives under ``project/tools/ffmpeg/`` (gitignored,
fetched during P10). Prepending its ``bin/`` to ``PATH`` at collection time --
before ``test_scrcpy`` evaluates ``shutil.which`` at import -- lets the plain
``./.venv/Scripts/pytest`` command exercise the real decode pipeline instead of
skipping it. A no-op when ffmpeg is already on PATH or the bundle is absent.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _bundled_ffmpeg_bin() -> str | None:
    """First ``tools/ffmpeg/**/bin`` dir that contains an ffmpeg executable."""
    for exe in _PROJECT_ROOT.glob("tools/ffmpeg/**/bin/ffmpeg*"):
        if exe.is_file():
            return str(exe.parent)
    return None


if shutil.which("ffmpeg") is None:
    _bin = _bundled_ffmpeg_bin()
    if _bin is not None:
        os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")

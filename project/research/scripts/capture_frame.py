"""Probe: can scrcpy capture the (FLAG_SECURE-flagged) 2048 board?

Grabs one frame via the project's ScrcpySource, reports its max channel
value (all-black <= 8 would mean FLAG_SECURE blanking) and saves a PNG.

Usage:
    python capture_frame.py [out.png]

Read-only with respect to src/ -- only imports it.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot2048.device.capture import ScrcpyConfig  # noqa: E402
from bot2048.device.scrcpy import AdbScrcpyRunner, ScrcpySource  # noqa: E402

ADB = str(
    Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe"
)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        PROJECT_ROOT / "research" / "raw" / "scrcpy_probe.png"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    cfg = ScrcpyConfig(ffmpeg_path="C:/ffmpeg/ffmpeg.exe")
    src = ScrcpySource(cfg, runner=AdbScrcpyRunner(adb_exe=ADB))
    src.start()
    try:
        frame = src.capture()
        mx = int(frame.max())
        print(f"frame shape={frame.shape} max_channel={mx}")
        if mx <= cfg.black_pixel_max:
            print("VERDICT: all-black -> FLAG_SECURE blanks scrcpy too")
        else:
            print("VERDICT: real pixels -> scrcpy sees the board")
        try:
            import cv2

            cv2.imwrite(str(out), frame[:, :, ::-1])
            print(f"saved {out}")
        except ImportError:
            import numpy as np

            np.save(str(out.with_suffix(".npy")), frame)
            print(f"cv2 missing; saved {out.with_suffix('.npy')}")
    finally:
        src.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

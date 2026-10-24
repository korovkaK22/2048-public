"""Swipe-throughput research harness for the monobank 2048 bot.

Measures how many swipes/sec each injection method sustains and how many
of the sent swipes the GAME actually accepts.

Move counting trick: the sum of all tile values is invariant under
slides/merges and grows by exactly the spawned tile (2 or 4, ~90%/10%)
per ACCEPTED move. So from a before/after board parse:

    delta = sum_after - sum_before
    accepted in [ceil(delta/4), floor(delta/2)],  estimate ~ delta/2.2

Modes (run one per invocation; each writes JSON to research/raw/):
    baseline  -- one `adb shell input swipe` subprocess per swipe (bot's
                 current method), dur=100, N=30
    persistent-- single `adb shell`, swipe commands streamed via stdin
    dursweep  -- persistent shell, dur in {300,100,50,20}, paced 150 ms,
                 acceptance per dur
    burst     -- persistent shell, dur=50, K swipes at pause intervals
                 {0,50,100,150,200} ms, acceptance per interval

Read-only w.r.t. src/ (imports only). All artefacts -> research/raw/.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot2048.device import ui_map  # noqa: E402
from bot2048.device.capture import ScrcpyConfig  # noqa: E402
from bot2048.device.scrcpy import AdbScrcpyRunner, ScrcpySource  # noqa: E402
from bot2048.vision.parser import parse_board  # noqa: E402
from bot2048.vision.states import (  # noqa: E402
    ScreenState,
    detect_state,
    load_calibration_bundle,
)

# PATH first (brew / apt / winget); the Windows SDK location is only a fallback.
ADB = shutil.which("adb") or str(
    Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe"
)
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
RAW_DIR = PROJECT_ROOT / "research" / "raw"
CAL_PATH = PROJECT_ROOT / "data" / "calibration.json"

# Swipes stay inside grid_bbox (107, 760, 973, 1625); ~300 px strokes
# around the board centre (540, 1192).
SWIPES: dict[str, tuple[int, int, int, int]] = {
    "up": (540, 1350, 540, 1050),
    "down": (540, 1050, 540, 1350),
    "left": (700, 1192, 400, 1192),
    "right": (400, 1192, 700, 1192),
}

RNG = random.Random(2048)


def rand_dirs(n: int) -> list[str]:
    return [RNG.choice(list(SWIPES)) for _ in range(n)]


# --------------------------------------------------------------- shell layer
class PersistentShell:
    """One `adb shell`; commands streamed over stdin, sync via echo markers."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [ADB, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def run(self, cmd: str, marker: str) -> None:
        """Send cmd + echo marker; block until the marker comes back."""
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(f"{cmd}; echo {marker}\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("adb shell died")
            if marker in line:
                return

    def swipe(self, d: str, dur: int, marker: str = "__M__") -> None:
        x1, y1, x2, y2 = SWIPES[d]
        self.run(f"input swipe {x1} {y1} {x2} {y2} {dur}", marker)

    def tap(self, xy: tuple[int, int]) -> None:
        self.run(f"input tap {xy[0]} {xy[1]}", "__T__")

    def close(self) -> None:
        if self.proc.stdin:
            try:
                self.proc.stdin.write("exit\n")
                self.proc.stdin.flush()
            except OSError:
                pass
        self.proc.terminate()


def baseline_swipe(d: str, dur: int) -> None:
    x1, y1, x2, y2 = SWIPES[d]
    subprocess.run(
        [ADB, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur)],
        check=True,
        capture_output=True,
    )


# -------------------------------------------------------------- vision layer
class Vision:
    def __init__(self) -> None:
        self.calibration, self.anchors = load_calibration_bundle(CAL_PATH)
        cfg = ScrcpyConfig(ffmpeg_path=FFMPEG)
        self.src = ScrcpySource(cfg, runner=AdbScrcpyRunner(adb_exe=ADB))
        self.src.start()

    def frame(self):
        return self.src.capture()

    def board_sum(self) -> int:
        board = parse_board(self.frame(), self.calibration, lenient=True)
        return sum(2**e for e in board if e > 0)

    def tiles(self) -> int:
        board = parse_board(self.frame(), self.calibration, lenient=True)
        return sum(1 for e in board if e > 0)

    def state(self) -> ScreenState:
        return detect_state(self.frame(), self.calibration, self.anchors).state

    def save_frame(self, name: str) -> None:
        import cv2

        RAW_DIR.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(RAW_DIR / name), self.frame()[:, :, ::-1])

    def restart_source(self) -> None:
        """Rebuild the scrcpy pipeline (stale black frame after screen wake)."""
        self.src.close()
        cfg = ScrcpyConfig(ffmpeg_path=FFMPEG)
        self.src = ScrcpySource(cfg, runner=AdbScrcpyRunner(adb_exe=ADB))
        self.src.start()

    def close(self) -> None:
        self.src.close()


def wake_device() -> None:
    """Wake the display via a one-shot adb call (before scrcpy starts)."""
    subprocess.run(
        [ADB, "shell", "input", "keyevent", "224"], check=True, capture_output=True
    )
    time.sleep(1.5)


def fresh_game(vision: Vision, shell: PersistentShell) -> None:
    """Restart the game so every phase starts from a 2-tile board."""
    state = vision.state()
    if state == ScreenState.SCREEN_OFF:
        shell.run("input keyevent 224", "__W__")  # KEYCODE_WAKEUP
        time.sleep(2.0)
        state = vision.state()
    if state == ScreenState.SCREEN_OFF:
        # awake per dumpsys but the stream is stale black -> rebuild pipeline
        vision.restart_source()
        time.sleep(1.0)
        state = vision.state()
    if state == ScreenState.WIN_POPUP_2048:
        shell.tap(ui_map.WIN_NEW_GAME)
        time.sleep(1.5)
        state = vision.state()
    if state == ScreenState.CONFIRM_NEW_GAME:
        shell.tap(ui_map.CONFIRM_YES)
        time.sleep(1.5)
        state = vision.state()
    if state != ScreenState.GAME:
        vision.save_frame("unexpected_state.png")
        raise RuntimeError(f"cannot reach GAME screen, state={state}")
    shell.tap(ui_map.NEW_GAME)
    time.sleep(1.2)
    if vision.state() == ScreenState.CONFIRM_NEW_GAME:
        shell.tap(ui_map.CONFIRM_YES)
        time.sleep(1.5)
    if vision.state() != ScreenState.GAME:
        vision.save_frame("unexpected_after_restart.png")
        raise RuntimeError("no GAME screen after restart")


def acceptance(sent: int, before: int, after: int) -> dict:
    delta = max(0, after - before)
    lo = -(-delta // 4)  # ceil
    hi = delta // 2
    est = min(sent, round(delta / 2.2)) if delta else 0
    return {
        "sent": sent,
        "sum_before": before,
        "sum_after": after,
        "sum_delta": delta,
        "accepted_min": lo,
        "accepted_max": min(sent, hi),
        "accepted_est": est,
    }


def write_json(name: str, payload: dict) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------- modes
def mode_baseline(n: int = 30, dur: int = 100) -> None:
    vision, shell = Vision(), PersistentShell()
    try:
        fresh_game(vision, shell)
        before = vision.board_sum()
        dirs = rand_dirs(n)
        t0 = time.perf_counter()
        for d in dirs:
            baseline_swipe(d, dur)
        elapsed = time.perf_counter() - t0
        time.sleep(1.5)
        after = vision.board_sum()
        payload = {
            "mode": "baseline",
            "dur_ms": dur,
            "n": n,
            "elapsed_s": elapsed,
            "swipes_per_s": n / elapsed,
            "ms_per_swipe": 1000 * elapsed / n,
            "acceptance": acceptance(n, before, after),
            "dirs": dirs,
        }
        print(json.dumps(payload, indent=2))
        write_json("baseline_dur100.json", payload)
    finally:
        shell.close()
        vision.close()


def mode_persistent(n: int = 30, dur: int = 100) -> None:
    vision, shell = Vision(), PersistentShell()
    try:
        fresh_game(vision, shell)
        before = vision.board_sum()
        dirs = rand_dirs(n)
        t0 = time.perf_counter()
        for i, d in enumerate(dirs):
            shell.swipe(d, dur, marker=f"__S{i}__")
        elapsed = time.perf_counter() - t0
        time.sleep(1.5)
        after = vision.board_sum()
        payload = {
            "mode": "persistent",
            "dur_ms": dur,
            "n": n,
            "elapsed_s": elapsed,
            "swipes_per_s": n / elapsed,
            "ms_per_swipe": 1000 * elapsed / n,
            "acceptance": acceptance(n, before, after),
            "dirs": dirs,
        }
        print(json.dumps(payload, indent=2))
        write_json("persistent_dur100.json", payload)
    finally:
        shell.close()
        vision.close()


def mode_dursweep(n: int = 30, pace_ms: int = 150) -> None:
    vision, shell = Vision(), PersistentShell()
    results = []
    try:
        for dur in (300, 100, 50, 20):
            fresh_game(vision, shell)
            before = vision.board_sum()
            dirs = rand_dirs(n)
            t0 = time.perf_counter()
            for i, d in enumerate(dirs):
                shell.swipe(d, dur, marker=f"__S{i}__")
                time.sleep(pace_ms / 1000)
            elapsed = time.perf_counter() - t0
            time.sleep(1.5)
            after = vision.board_sum()
            res = {
                "dur_ms": dur,
                "n": n,
                "pace_ms": pace_ms,
                "elapsed_s": elapsed,
                "swipes_per_s": n / elapsed,
                "acceptance": acceptance(n, before, after),
            }
            results.append(res)
            print(json.dumps(res, indent=2))
        write_json("dursweep.json", {"mode": "dursweep", "results": results})
    finally:
        shell.close()
        vision.close()


def mode_burst(k: int = 20, dur: int = 50) -> None:
    vision, shell = Vision(), PersistentShell()
    results = []
    try:
        for pause_ms in (0, 50, 100, 150, 200):
            fresh_game(vision, shell)
            before = vision.board_sum()
            dirs = rand_dirs(k)
            t0 = time.perf_counter()
            for i, d in enumerate(dirs):
                shell.swipe(d, dur, marker=f"__S{i}__")
                if pause_ms:
                    time.sleep(pause_ms / 1000)
            elapsed = time.perf_counter() - t0
            time.sleep(1.5)
            after = vision.board_sum()
            res = {
                "pause_ms": pause_ms,
                "dur_ms": dur,
                "k": k,
                "elapsed_s": elapsed,
                "swipes_per_s": k / elapsed,
                "acceptance": acceptance(k, before, after),
            }
            results.append(res)
            print(json.dumps(res, indent=2))
        write_json("burst_intervals.json", {"mode": "burst", "results": results})
    finally:
        shell.close()
        vision.close()


def main() -> int:
    wake_device()
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    {
        "baseline": mode_baseline,
        "persistent": mode_persistent,
        "dursweep": mode_dursweep,
        "burst": mode_burst,
    }[mode]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``bot2048 live`` sub-app: live play and spawn research on a real phone.

Commands (host only, never Docker; see docs/live-setup.md):

* ``bot2048 live play``     -- the real loop under the full lifecycle guard
  (run_id + lock + restart budget + heartbeat + Telegram bridge).
* ``bot2048 live research`` -- spawn-rule measurement (cheap policy, many
  games), same guard.
* ``bot2048 live once``     -- DRY debug run: one capture -> state ->
  (parse -> decide), prints the decision, NEVER swipes or taps.

The concrete vision implementation (``detect_state`` / ``parse_board``) is
bound to the calibration HERE and injected into the runner behind its local
:class:`~bot2048.runner.live.StateDetector` / ``BoardReader`` protocols --
the loop itself never imports the parser, so vision/ can keep moving.
"""

from __future__ import annotations

import importlib
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

import structlog
import typer
from rich.console import Console

from bot2048.ai import CornerStrategy, ExpectimaxStrategy
from bot2048.ai.go_engine import GoEngineError, GoExpectimaxStrategy
from bot2048.ai.strategies import Strategy
from bot2048.core.engine import is_game_over
from bot2048.core.types import Board, board_values
from bot2048.device.adb import AdbDevice, DeviceError
from bot2048.device.apk_capture import ApkBoardSource
from bot2048.device.capture import StreamingCaptureDevice, make_frame_source
from bot2048.device.config import DeviceConfig
from bot2048.device.humanizer import SWIPE_MS_ENV_VAR
from bot2048.device.supervisor import Supervisor
from bot2048.logging_setup import setup_logging, stop_logging
from bot2048.runner.escalation import probe_screen
from bot2048.runner.incidents import Image
from bot2048.runner.lifecycle import (
    LockHeldError,
    RestartLoopError,
    RunContext,
    run_session,
)
from bot2048.runner.live import (
    SETTLE_DELAY_DEFAULT_S,
    CellLook,
    LiveConfig,
    LiveRunner,
    NotifyHook,
)
from bot2048.runner.live_config import LIVE_SWIPE_DURATION_MS, SETTLE_DELAY_APK_S
from bot2048.runner.research import ResearchConfig, ResearchRunner, make_policy
from bot2048.storage.sqlite_recorder import ExperimentMeta, SqliteRecorder
from bot2048.tgnotify import get_notifier
from bot2048.vision.calibration import (
    DEFAULT_CALIBRATION_PATH,
    CalibrationError,
)
from bot2048.vision.parser import (
    BoardParseError,
    cell_crop,
    cell_half_size,
    learn_tile_color,
    parse_board,
    parse_board_detailed,
)
from bot2048.vision.states import (
    ScreenState,
    StateDetection,
    detect_state,
    load_calibration_bundle,
)

app = typer.Typer(
    name="live",
    no_args_is_help=True,
    help="Live Android player (host only, never Docker).",
)
console = Console()
err_console = Console(stderr=True)

log = structlog.get_logger(__name__)

_STRATEGY_NAMES = ("expectimax", "go", "greedy", "corner", "random")
_CAPTURE_KINDS = ("scrcpy", "screencap", "apk")

#: Repo-bundled binaries/artifacts (all gitignored), relative to project/.
#: Only ``adb`` needs the OS-conditional suffix here; ffmpeg is resolved
#: PATH-first (see :func:`_resolve_ffmpeg_exe`) and the scrcpy-server jar is
#: resolved per-host by :func:`_resolve_scrcpy_server_jar`.
_BUNDLED_ADB = Path("tools") / "platform-tools" / ("adb.exe" if sys.platform == "win32" else "adb")
_BUNDLED_FFMPEG = Path("tools") / "ffmpeg" / "ffmpeg-master-latest-win64-gpl" / "bin" / "ffmpeg.exe"

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

LEARNED_TILES_DIR: Path = Path("logs") / "learned_tiles"
"""Best-effort PNG crops of every auto-learned tile (human audit trail)."""


class _DetectFn(Protocol):
    def __call__(self, image: Image) -> StateDetection: ...


class _ReadFn(Protocol):
    def __call__(self, image: Image) -> Board: ...


class _Runner(Protocol):
    def run(self) -> None: ...


# The game-end notification attaches an image, so the hook accepts an optional
# photo path (see live.NotifyHook); ``tg.notify`` already supports it.
_NotifyFn = NotifyHook
_BuildFn = Callable[[RunContext, SqliteRecorder, Supervisor, _NotifyFn], _Runner]


class _Vision:
    """Calibration-bound vision functions with a SWAPPABLE calibration.

    ``learn_tile`` (the runner's auto-learn hook) replaces
    :attr:`calibration` in place after ``vision.learn_tile_color`` accepts a
    new tile color, so ``detect``/``read``/``read_cells`` immediately parse
    with the extended palette -- and the JSON at ``path`` is persisted by
    vision itself, surviving restarts.
    """

    def __init__(self, path: Path) -> None:
        try:
            self.calibration, self._anchors = load_calibration_bundle(path)
        except CalibrationError as exc:
            err_console.print(f"[red]calibration error:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        self.path = path

    def detect(self, image: Image) -> StateDetection:
        return detect_state(image, self.calibration, self._anchors)

    def read(self, image: Image) -> Board:
        # Lenient: a fully unknown tile reads as exponent 0 instead of
        # raising, so live.py reaches its mismatch branch and the
        # auto-learn gate (_try_learn_new_tiles) instead of aborting the
        # whole game with VISION_MISMATCH.
        return parse_board(image, self.calibration, lenient=True)

    def read_cells(self, image: Image) -> tuple[CellLook, ...] | None:
        """Per-cell trust info for auto-learn (``None`` = no detail available)."""
        try:
            reads = parse_board_detailed(image, self.calibration, lenient=True)
        except (BoardParseError, ValueError):
            return None
        return tuple(
            CellLook(exponent=r.exponent, suspect=r.method != "digits") for r in reads
        )

    def learn_tile(self, frame: Image, cell: int, exponent: int) -> bool:
        """Teach one tile color; ``False`` = vision refused the sample."""
        try:
            self.calibration = learn_tile_color(
                self.calibration, frame, cell, exponent, path=self.path
            )
        except BoardParseError:
            return False
        self._save_learned_crop(frame, cell, exponent)
        return True

    def _save_learned_crop(self, frame: Image, cell: int, exponent: int) -> None:
        """Best-effort PNG of the just-learned cell crop.

        Saved under ``logs/learned_tiles/`` so a human can audit what color
        sample entered the palette. Mirrors the incident-bundle rule: an
        artifact failure is logged and skipped -- it must NEVER undo or mask
        an accepted learn (the calibration is already persisted).
        """
        try:
            crop = cell_crop(
                frame,
                self.calibration.cell_centers[cell],
                cell_half_size(self.calibration),
            )
            LEARNED_TILES_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = LEARNED_TILES_DIR / f"{stamp}_cell{cell:02d}_value{2**exponent}.png"
            pil_image = importlib.import_module("PIL.Image")
            pil_image.fromarray(crop).save(path)
            log.info(
                "live.tile_crop_saved", file=str(path), cell=cell, exponent=exponent
            )
        except Exception as exc:  # Pillow missing / crop error -- never fatal
            log.warning("live.tile_crop_failed", cell=cell, error=str(exc))


def _make_vision(path: Path) -> _Vision:
    """Load calibration + state anchors and bind the concrete vision functions."""
    return _Vision(path)


def _make_strategy(
    name: str,
    depth: int,
    farm: bool = False,
    prefetch: bool = False,
    budget_ms: float | None = None,
) -> Strategy:
    if name == "expectimax":
        return ExpectimaxStrategy(depth=depth)
    if name == "go":
        # Fast Go bitboard expectimax (tools/engine2048); persistent
        # subprocess reused across every move. Fills the same per-move
        # telemetry (eval_ms / search_depth / eval_details) as the Python one.
        # ``farm`` switches to the monobank stop-rule objective (2048 = game
        # end; maximise the board S-sum at the stopping moment).
        # ``budget_ms`` switches to time-budgeted iterative deepening
        # (``depth`` becomes a ceiling); ``None`` keeps fixed-depth search.
        try:
            return GoExpectimaxStrategy(
                depth=depth, farm=farm, budget_ms=budget_ms, prefetch=prefetch
            )
        except (GoEngineError, RuntimeError) as exc:
            err_console.print(f"[red]go engine unavailable:[/red] {exc}")
            err_console.print(
                "build it with tools/engine2048/build.sh (needs Go on PATH) "
                "or point BOT2048_ENGINE2048_BIN at the binary"
            )
            raise typer.Exit(code=1) from exc
    if farm:
        err_console.print(
            f"[yellow]note:[/yellow] --farm only applies to the 'go' strategy; "
            f"ignored for {name!r}."
        )
    if budget_ms is not None:
        err_console.print(
            f"[yellow]note:[/yellow] --budget-ms only applies to the 'go' strategy; "
            f"ignored for {name!r}."
        )
    if name == "corner":
        return CornerStrategy()
    if name in ("random", "greedy"):
        return make_policy(name)
    err_console.print(
        f"[red]unknown strategy:[/red] {name!r} (expected one of {', '.join(_STRATEGY_NAMES)})"
    )
    raise typer.Exit(code=1)


def _connect(serial: str | None) -> AdbDevice:
    device = AdbDevice(DeviceConfig(serial=serial))
    try:
        device.connect()
    except DeviceError as exc:
        err_console.print(f"[red]device error:[/red] {exc}")
        err_console.print("check `adb devices` / docs/live-setup.md")
        raise typer.Exit(code=1) from exc
    return device


def _resolve_adb_exe() -> str:
    """Repo-local adb (``tools/platform-tools``, gitignored) if present, else PATH."""
    return str(_BUNDLED_ADB) if _BUNDLED_ADB.exists() else "adb"


def _resolve_ffmpeg_exe() -> str:
    """PATH ffmpeg if available, else the repo-bundled exe, else plain ``ffmpeg``.

    The last case lets :class:`~bot2048.device.scrcpy.ScrcpySource` surface
    the spawn error itself -- the capture fallback then keeps the run alive
    on screencap.
    """
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    if _BUNDLED_FFMPEG.exists():
        return str(_BUNDLED_FFMPEG)
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


def _run_guarded(
    *,
    device: AdbDevice | StreamingCaptureDevice,
    detect: _DetectFn,
    db: Path,
    args: dict[str, object],
    build: _BuildFn,
    experiment: str,
    strategy_name: str,
    params: dict[str, object],
    verbose: bool = False,
) -> None:
    """run_session + Telegram bracket + Supervisor/Recorder wiring (§9.8).

    On FatalIncident run_session raises SystemExit(2) with the bundle/alert
    already on disk; the Telegram bracket still closes in ``finally``.
    typer.Exit is never raised inside the session (it would count as CRASH).

    Logging is configured HERE (not in ``play``/``research``) so the CLI unit
    tests -- which stub this function -- never start the async log pump. The
    per-move hot-path debug lines are gated off unless ``verbose`` selects
    DEBUG; :func:`stop_logging` in the ``finally`` flushes the background queue
    so a FATAL loses nothing.
    """
    setup_logging(json_logs=True, level=logging.DEBUG if verbose else logging.INFO)
    tg = get_notifier()
    tg.start()
    ok = False
    try:
        with run_session(db_path=db, args=dict(args)) as ctx:
            ctx.alerts.notifier = tg.notify_alert  # ERROR/FATAL -> Telegram (§3.2)
            tg.session_start(ctx.run_id, strategy_name)
            supervisor = Supervisor(
                device,
                detect,
                ctx.alerts,
                run_id=ctx.run_id,
                ops=ctx.ops,
                ring=ctx.ring,
            )
            recorder = SqliteRecorder(
                db,
                ExperimentMeta(
                    experiment_id=experiment,
                    kind="live",
                    strategy=strategy_name,
                    params=dict(params),
                ),
            )
            try:
                runner = build(ctx, recorder, supervisor, tg.notify)
                runner.run()
            finally:
                recorder.close()
        ok = True
    except (LockHeldError, RestartLoopError) as exc:
        err_console.print(f"[red]refusing to start:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    finally:
        tg.session_end(ok, reason="" if ok else "див. logs/alerts.jsonl")
        tg.stop(flush_timeout=5.0)
        stop_logging()  # drain the async log queue so no line is lost on exit


@app.command()
def play(
    strategy: Annotated[
        str,
        typer.Option("--strategy", help="Strategy name (expectimax|go|greedy|corner|random)."),
    ] = "expectimax",
    depth: Annotated[
        int, typer.Option("--depth", min=1, help="Search depth (expectimax / go).")
    ] = 3,
    farm: Annotated[
        bool,
        typer.Option(
            "--farm",
            help="Farm mode (go strategy only): monobank stop-rule objective — "
            "never merge to 2048 unless forced; maximise the board score at stop.",
        ),
    ] = False,
    games: Annotated[
        int,
        typer.Option("--games", min=0, help="Games to play (0 = indefinitely, until killed)."),
    ] = 1,
    db: Annotated[
        Path, typer.Option("--db", help="SQLite database for full game telemetry.")
    ] = Path("data") / "games.db",
    calibration: Annotated[
        Path, typer.Option("--calibration", "-c", help="Calibration JSON.")
    ] = DEFAULT_CALIBRATION_PATH,
    serial: Annotated[
        str | None, typer.Option("--serial", help="adb serial (default: the only device).")
    ] = None,
    experiment: Annotated[
        str, typer.Option("--experiment", help="Experiment id grouping the games.")
    ] = "live",
    auto_login: Annotated[
        bool,
        typer.Option(
            "--auto-login",
            help="Opt-in PIN entry on the auth wall (requires MONO_PIN; default: "
            "AUTH_REQUIRED -> FATAL, a human unlocks).",
        ),
    ] = False,
    start_grace: Annotated[
        float,
        typer.Option(
            "--start-grace",
            min=0.0,
            help="Hands-off countdown (seconds) before the first swipe: notifies "
            "'starting in N seconds' and waits, ignoring the phone. 0 disables.",
        ),
    ] = 10.0,
    capture: Annotated[
        str,
        typer.Option(
            "--capture",
            help="Frame capture path: scrcpy (fast H.264 stream; automatic "
            "fallback to screencap) | screencap (~0.65 s/frame, always safe) | "
            "apk (on-device sampler APK: 22-byte board packets over adb forward, "
            "~50 ms/move; detect_state stays on screencap).",
        ),
    ] = "scrcpy",
    prefetch: Annotated[
        bool,
        typer.Option(
            "--prefetch/--no-prefetch",
            help="Speculative precompute (go strategy only): search the next move "
            "during the animation/perception window and serve it from a lookup on "
            "hit. Decisions are identical to a normal search (pure latency win); "
            "the engine-side pacing gate self-disables it if calls come too fast. "
            "LIVE-only.",
        ),
    ] = True,
    budget_ms: Annotated[
        float | None,
        typer.Option(
            "--budget-ms",
            min=1e-9,
            help="Per-move time budget in ms (go strategy only): switches the "
            "engine to time-budgeted iterative deepening (--depth becomes a "
            "ceiling); deeper search hides under the prefetch window. Unset "
            "keeps fixed-depth search.",
        ),
    ] = None,
    reset_on_start: Annotated[
        bool,
        typer.Option(
            "--reset-on-start/--no-reset-on-start",
            help="Tap «Нова гра» once at session start (#46) to ZERO the in-game "
            "timer before the first game. An auth wall on the route is cleared "
            "via the existing opt-in auto-login (--auto-login + MONO_PIN). "
            "Best-effort: an unsafe screen, a device error, or a board that "
            "never verifies fresh only warns and the current game is "
            "played instead (never crashes). The in-play «Нова гра» coordinate "
            "may need live calibration per device.",
        ),
    ] = True,
    resume_on_start: Annotated[
        bool,
        typer.Option(
            "--resume-on-start/--no-resume-on-start",
            help="RESUME the in-progress game already on the phone at session "
            "start instead of tapping «Нова гра» and wiping it (P5: lossless "
            "crash-restarts). Overrides --reset-on-start ONLY when a healthy "
            "GAME screen shows a non-fresh, non-dead board; in every other "
            "case (fresh board, popup/off/unknown screen, probe trouble) the "
            "normal reset/adopt behavior runs unchanged. An auth wall is "
            "first cleared via the existing opt-in auto-login (--auto-login "
            "+ MONO_PIN). The resumed game's timer restarts from now (honest "
            "lower bound).",
        ),
    ] = False,
    settle_delay: Annotated[
        float | None,
        typer.Option(
            "--settle-delay",
            min=0.0,
            help="Post-swipe animation wait (seconds) before the first "
            "verification capture (LiveConfig.settle_delay_s). Live A/B knob for "
            "trading move latency vs settle stability. Unset picks a per-capture "
            "default: 0.05 s for --capture apk (the packet gate tolerates early "
            "polls and accepts the settled spawn packet when it lands), 0.13 s "
            "for the frame paths (scrcpy/screencap).",
        ),
    ] = None,
    swipe_duration: Annotated[
        int | None,
        typer.Option(
            "--swipe-duration",
            min=1,
            help="Swipe gesture duration (ms), fixed/jitter-free, overriding the "
            "pacing profile default (fast = 80 ms). Live A/B knob for swipe speed "
            "vs the move_rejected retry rate. Unset resolves to the live default "
            "of 45 ms (LIVE_SWIPE_DURATION_MS; game accepts >=~20 ms per "
            "research/swipe_throughput.md) unless BOT2048_SWIPE_MS is set, "
            "which takes precedence over the unset default.",
        ),
    ] = None,
    swipe_retries: Annotated[
        int,
        typer.Option(
            "--swipe-retries",
            min=0,
            help="How many times to re-swipe when the board is unchanged "
            "(verify-and-retry). Total attempts = this + 1; after all fail the "
            "move is MOVE_REJECTED and the game aborts. Live A/B knob for a "
            "flaky-tap device (LiveConfig.swipe_retries).",
        ),
    ] = 4,
    swipe_retry_delay: Annotated[
        float,
        typer.Option(
            "--swipe-retry-delay",
            min=0.0,
            help="Pause (seconds) between rejected-swipe retries "
            "(LiveConfig.swipe_retry_delay_s). Live A/B knob for pacing the "
            "re-swipe when a tap does not register.",
        ),
    ] = 0.25,
    stop_before_win: Annotated[
        bool,
        typer.Option(
            "--stop-before-win/--no-stop-before-win",
            help="Safe brink mode: the moment the chosen move WOULD create a 2048 "
            "tile, do NOT swipe -- finalize the game as 'held_before_win' and STOP "
            "(no autorestart), leaving the phone one swipe from 2048 for a human to "
            "make the final merge and read the score. Default OFF (campaigns must "
            "never accidentally hold).",
        ),
    ] = False,
    escalate_after_ply: Annotated[
        int | None,
        typer.Option(
            "--escalate-after-ply",
            min=1,
            help="Mid-game budget escalation: once a game reaches this ply, raise "
            "the go engine's budget to --escalate-budget-ms (brief §5.2 median "
            "trigger). Needs --budget-ms and --escalate-budget-ms.",
        ),
    ] = None,
    escalate_when_empty_le: Annotated[
        int | None,
        typer.Option(
            "--escalate-when-empty-le",
            min=0,
            max=15,
            help="Late-game trigger: escalate budget once empty cells <= this "
            "(low-branching endgame).",
        ),
    ] = None,
    escalate_budget_ms: Annotated[
        float | None,
        typer.Option(
            "--escalate-budget-ms",
            min=1e-9,
            help="The raised per-move budget (ms, e.g. 800) used after an "
            "escalation trigger fires. No effect unless a trigger and "
            "--budget-ms are set. Legacy single-step knob: maps onto the "
            "3-tier ladder (--escalate-after-ply -> T1, "
            "--escalate-when-empty-le -> T2, both at this budget).",
        ),
    ] = None,
    late_after_ply: Annotated[
        int | None,
        typer.Option(
            "--late-after-ply",
            min=1,
            help="Budget ladder T1 trigger (brief §3): once a game reaches this "
            "ply (champion median ~2067), raise the go engine's budget to "
            "--late-budget-ms. Needs --budget-ms and --late-budget-ms.",
        ),
    ] = None,
    late_budget_ms: Annotated[
        float | None,
        typer.Option(
            "--late-budget-ms",
            min=1e-9,
            help="Budget ladder T1 per-move budget (ms, brief §3: ~2000) applied "
            "past --late-after-ply -- trade speed for depth once we are going "
            "for the record.",
        ),
    ] = None,
    endgame_when_empty_le: Annotated[
        int | None,
        typer.Option(
            "--endgame-when-empty-le",
            min=0,
            max=15,
            help="Budget ladder T2 trigger (brief §3): step to "
            "--endgame-budget-ms once empty cells <= this (death-zone / near "
            "cash-out, e.g. 3). Monotonic: never steps back down mid-game.",
        ),
    ] = None,
    endgame_budget_ms: Annotated[
        float | None,
        typer.Option(
            "--endgame-budget-ms",
            min=1e-9,
            help="Budget ladder T2 per-move budget (ms, brief §3: up to ~9000) "
            "for the deepest endgame search. Must be >= --late-budget-ms when "
            "both are set.",
        ),
    ] = None,
    endgame_after_ply: Annotated[
        int | None,
        typer.Option(
            "--endgame-after-ply",
            min=1,
            help="Budget ladder T2 MOVE-COUNT gate: the endgame tier fires only "
            "once ply >= this AND empties <= --endgame-when-empty-le, so an early "
            "transient low-empties dip (a fresh board hits <=4 empty around ply "
            "20-40) can never trigger T2. Unset defaults to --late-after-ply "
            "(the median); set higher to gate T2 even later.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose/--no-verbose",
            "-v",
            help="Log at DEBUG instead of the default INFO: brings back the "
            "high-frequency per-move debug lines (adb.swipe, live.apk_gate_hit / "
            "live.predict_gate_hit, vision.cell.color_fallback) that are gated "
            "off the hot path by default. INFO keeps the per-move "
            "live.move_timing cadence line and all game/incident events.",
        ),
    ] = False,
) -> None:
    """Play 2048 in the monobank app via ADB (full lifecycle guard)."""
    if budget_ms is None and (
        escalate_after_ply is not None
        or escalate_when_empty_le is not None
        or escalate_budget_ms is not None
        or late_after_ply is not None
        or late_budget_ms is not None
        or endgame_when_empty_le is not None
        or endgame_budget_ms is not None
        or endgame_after_ply is not None
    ):
        console.print(
            "[yellow]note:[/yellow] --escalate-*/--late-*/--endgame-* only act "
            "on a budget-mode go strategy; without --budget-ms the ladder "
            "hook no-ops."
        )
    vision = _make_vision(calibration)
    strat = _make_strategy(strategy, depth, farm=farm, prefetch=prefetch, budget_ms=budget_ms)
    if strategy == "go" and not prefetch:
        # Speculative prefetch is the ONLY thing that hides the engine's
        # next-move search under the swipe/settle window (go_engine.py
        # _maybe_speculate/lookup); without it every search lands on the
        # per-move critical path. Decisions are identical either way, so
        # --no-prefetch is purely a latency loss on live (the engine-side
        # pacing gate already self-disables speculation when calls come
        # back-to-back, e.g. sim benches -- live is never that fast).
        console.print(
            "[yellow]note:[/yellow] --no-prefetch puts the full engine search on "
            "the per-move critical path; with --prefetch the next move is "
            "precomputed during the swipe/settle window (identical decisions, "
            "pure latency win)."
        )
    device = _connect(serial)
    capture_device, capture_owner = _make_capture_device(device, capture, serial)
    board_source = _make_board_source(capture, serial)
    if capture_owner is not None:
        _ensure_scrcpy_server_pushed(_resolve_adb_exe(), serial)
    if settle_delay is None:
        # Per-capture default: the APK packet gate accepts the settled spawn
        # packet whenever it lands (early polls just wait inside the gate
        # budget), so the blind sleep need not outlast the ~115 ms animation
        # the way the frame paths must. Explicit --settle-delay wins always.
        settle_delay = SETTLE_DELAY_APK_S if capture == "apk" else SETTLE_DELAY_DEFAULT_S
    if swipe_duration is None and not os.environ.get(SWIPE_MS_ENV_VAR, "").strip():
        # Live default: 45 ms gestures instead of the pacing profile's 80 ms
        # fast tier. The game registers swipes down to ~20 ms
        # (research/swipe_throughput.md), so 45 ms keeps >2x margin while
        # cutting ~35 ms of gesture time per move. Explicit --swipe-duration
        # or BOT2048_SWIPE_MS always wins; the verify-and-retry path
        # (--swipe-retries / move_rejected) stays as the safety net and this
        # default must be A/B'd against that retry rate.
        swipe_duration = LIVE_SWIPE_DURATION_MS
    config = LiveConfig(
        experiment_id=experiment,
        strategy_name=strat.name,
        max_games=games,
        auto_login=auto_login,
        start_grace_s=start_grace,
        reset_on_start=reset_on_start,
        resume_on_start=resume_on_start,
        settle_delay_s=settle_delay,
        swipe_duration_ms=swipe_duration,
        swipe_retries=swipe_retries,
        swipe_retry_delay_s=swipe_retry_delay,
        stop_before_win=stop_before_win,
        escalate_after_ply=escalate_after_ply,
        escalate_when_empty_le=escalate_when_empty_le,
        escalate_budget_ms=escalate_budget_ms,
        late_after_ply=late_after_ply,
        late_budget_ms=late_budget_ms,
        endgame_when_empty_le=endgame_when_empty_le,
        endgame_budget_ms=endgame_budget_ms,
        endgame_after_ply=endgame_after_ply,
    )
    if strategy == "go" and prefetch and budget_ms is not None:
        # Prefetch only hides search that FITS inside the swipe+settle busy
        # window; a base budget above that window spills the remainder onto
        # the per-move critical path. Warn (never clamp -- ladder tiers and
        # deliberate deep-base configs may exceed it on purpose).
        window_ms = config.ponder_window_ms()
        if budget_ms > window_ms:
            console.print(
                f"[yellow]note:[/yellow] base --budget-ms {budget_ms:.0f} exceeds "
                f"the estimated prefetch window ~{window_ms:.0f} ms "
                "(swipe + settle + verify floor); the overflow lands on the "
                "per-move critical path. Recommended: keep the BASE budget <= "
                "the window and buy depth via --escalate/--late/--endgame "
                "tiers instead."
            )

    def build(
        ctx: RunContext, recorder: SqliteRecorder, supervisor: Supervisor, notify: _NotifyFn
    ) -> _Runner:
        return LiveRunner(
            device=capture_device,
            detect_state=vision.detect,
            read_board=vision.read,
            board_source=board_source,
            strategy=strat,
            recorder=recorder,
            supervisor=supervisor,
            alerts=ctx.alerts,
            grid_bbox=vision.calibration.grid_bbox,
            config=config,
            run_id=ctx.run_id,
            ops=ctx.ops,
            heartbeat=ctx.heartbeat,
            ring=ctx.ring,
            notify=notify,
            read_cells=vision.read_cells,
            learn_tile=vision.learn_tile,
            reconnect=device.connect,
        )

    try:
        _run_guarded(
            device=capture_device,
            detect=vision.detect,
            db=db,
            args={
                "command": "play",
                "strategy": strat.name,
                "games": games,
                "serial": serial,
                "experiment": experiment,
                "auto_login": auto_login,
                "capture": capture,
                "prefetch": prefetch,
                "budget_ms": budget_ms,
            },
            build=build,
            experiment=experiment,
            strategy_name=strat.name,
            params={
                "games": games,
                "command": "play",
                "depth": depth,
                "farm": farm,
                "capture": capture,
                "prefetch": prefetch,
                "budget_ms": budget_ms,
            },
            verbose=verbose,
        )
    finally:
        if capture_owner is not None:
            capture_owner.close()
        if board_source is not None:
            board_source.close()
    console.print(f"[green]done:[/green] telemetry in [bold]{db}[/bold]")


@app.command()
def research(
    games: Annotated[
        int, typer.Option("--games", min=1, help="Games to collect spawn samples from.")
    ] = 20,
    policy: Annotated[str, typer.Option("--policy", help="Move policy: random|greedy.")] = "random",
    db: Annotated[
        Path, typer.Option("--db", help="SQLite database for the collected spawns.")
    ] = Path("data") / "games.db",
    calibration: Annotated[
        Path, typer.Option("--calibration", "-c", help="Calibration JSON.")
    ] = DEFAULT_CALIBRATION_PATH,
    serial: Annotated[
        str | None, typer.Option("--serial", help="adb serial (default: the only device).")
    ] = None,
    experiment: Annotated[
        str, typer.Option("--experiment", help="Experiment id grouping the games.")
    ] = "spawn-research",
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose/--no-verbose",
            "-v",
            help="Log at DEBUG instead of the default INFO (per-move debug lines).",
        ),
    ] = False,
) -> None:
    """Measure spawn rules (2 vs 4, initial tiles) empirically."""
    vision = _make_vision(calibration)
    try:
        make_policy(policy)  # validate before touching the device
        config = ResearchConfig(experiment_id=experiment, games=games, move_policy=policy)
    except ValueError as exc:
        err_console.print(f"[red]bad option:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    device = _connect(serial)

    def build(
        ctx: RunContext, recorder: SqliteRecorder, supervisor: Supervisor, notify: _NotifyFn
    ) -> _Runner:
        return ResearchRunner(
            device=device,
            detect_state=vision.detect,
            read_board=vision.read,
            recorder=recorder,
            supervisor=supervisor,
            alerts=ctx.alerts,
            grid_bbox=vision.calibration.grid_bbox,
            config=config,
            run_id=ctx.run_id,
            ops=ctx.ops,
            heartbeat=ctx.heartbeat,
            ring=ctx.ring,
            notify=notify,
        )

    _run_guarded(
        device=device,
        detect=vision.detect,
        db=db,
        args={
            "command": "research",
            "policy": policy,
            "games": games,
            "serial": serial,
            "experiment": experiment,
        },
        build=build,
        experiment=experiment,
        strategy_name=policy,
        params={"games": games, "command": "research"},
        verbose=verbose,
    )
    console.print(f"[green]done:[/green] spawn samples in [bold]{db}[/bold]")


@app.command()
def once(
    calibration: Annotated[
        Path, typer.Option("--calibration", "-c", help="Calibration JSON.")
    ] = DEFAULT_CALIBRATION_PATH,
    serial: Annotated[
        str | None, typer.Option("--serial", help="adb serial (default: the only device).")
    ] = None,
    strategy: Annotated[
        str,
        typer.Option("--strategy", help="Strategy name (expectimax|go|greedy|corner|random)."),
    ] = "expectimax",
    depth: Annotated[
        int, typer.Option("--depth", min=1, help="Search depth (expectimax / go).")
    ] = 3,
    farm: Annotated[
        bool,
        typer.Option(
            "--farm",
            help="Farm mode (go strategy only): monobank stop-rule objective.",
        ),
    ] = False,
    budget_ms: Annotated[
        float | None,
        typer.Option(
            "--budget-ms",
            min=1e-9,
            help="Per-move time budget in ms (go strategy only): switches the "
            "engine to time-budgeted iterative deepening (--depth becomes a "
            "ceiling); deeper search hides under the prefetch window. Unset "
            "keeps fixed-depth search.",
        ),
    ] = None,
) -> None:
    """DRY debug run: capture -> state -> (parse -> decide). NO swipe, NO taps."""
    vision = _make_vision(calibration)
    device = _connect(serial)
    probe = probe_screen(device, vision.detect)
    console.print(
        f"state: [bold]{probe.state.value}[/bold] ({probe.reason}), "
        f"board_match_frac={probe.board_match_frac:.2f}"
    )
    if probe.state is not ScreenState.GAME or probe.image is None:
        err_console.print("[yellow]not on a healthy GAME screen -- nothing to decide[/yellow]")
        raise typer.Exit(code=1)
    board = vision.read(probe.image)
    values = board_values(board)
    for row in range(4):
        console.print("  " + " ".join(f"{values[row * 4 + col]:>5d}" for col in range(4)))
    if is_game_over(board):
        console.print("[bold red]game over[/bold red] (per core.engine.is_game_over)")
        return
    strat = _make_strategy(strategy, depth, farm=farm, budget_ms=budget_ms)
    decision = strat.choose(board)
    console.print(
        f"decision: [bold]{decision.direction.name}[/bold] "
        f"(strategy={strat.name}, eval_ms={decision.eval_ms:.1f}, depth={decision.search_depth})"
    )
    console.print("[dim]dry run: no swipe was sent[/dim]")

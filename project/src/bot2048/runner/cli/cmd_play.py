"""``bot2048 live play`` -- the real loop under the full lifecycle guard.

run_id + lock + restart budget + heartbeat + Telegram bridge; see
:func:`bot2048.runner.cli.guard._run_guarded`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from bot2048.device.humanizer import SWIPE_MS_ENV_VAR
from bot2048.device.supervisor import Supervisor
from bot2048.runner.cli.capture import _make_board_source, _make_capture_device
from bot2048.runner.cli.common import _connect, _NotifyFn, _Runner, console
from bot2048.runner.cli.guard import _run_guarded
from bot2048.runner.cli.strategy import _make_strategy
from bot2048.runner.cli.tools import _ensure_scrcpy_server_pushed, _resolve_adb_exe
from bot2048.runner.cli.vision_bind import _make_vision
from bot2048.runner.lifecycle import RunContext
from bot2048.runner.live import SETTLE_DELAY_DEFAULT_S, LiveConfig, LiveRunner
from bot2048.runner.live_config import LIVE_SWIPE_DURATION_MS, SETTLE_DELAY_APK_S
from bot2048.storage.sqlite_recorder import SqliteRecorder
from bot2048.vision.calibration import DEFAULT_CALIBRATION_PATH


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
            "docs/research/swipe_throughput.md) unless BOT2048_SWIPE_MS is set, "
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
        # (docs/research/swipe_throughput.md), so 45 ms keeps >2x margin while
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

"""``bot2048 live once`` -- DRY debug run: one capture -> state -> (parse -> decide).

Prints the decision and NEVER swipes or taps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bot2048.core.engine import is_game_over
from bot2048.core.types import board_values
from bot2048.runner.cli.common import _connect, console, err_console
from bot2048.runner.cli.strategy import _make_strategy
from bot2048.runner.cli.vision_bind import _make_vision
from bot2048.runner.escalation import probe_screen
from bot2048.vision.calibration import DEFAULT_CALIBRATION_PATH
from bot2048.vision.states import ScreenState


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

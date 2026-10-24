"""``bot2048 live research`` -- spawn-rule measurement (cheap policy, many games).

Runs under the same lifecycle guard as ``play``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bot2048.device.supervisor import Supervisor
from bot2048.runner.cli.common import _connect, _NotifyFn, _Runner, console, err_console
from bot2048.runner.cli.guard import _run_guarded
from bot2048.runner.cli.vision_bind import _make_vision
from bot2048.runner.lifecycle import RunContext
from bot2048.runner.research import ResearchConfig, ResearchRunner, make_policy
from bot2048.storage.sqlite_recorder import SqliteRecorder
from bot2048.vision.calibration import DEFAULT_CALIBRATION_PATH


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

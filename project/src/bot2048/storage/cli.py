"""``bot2048 db`` — reporting, replay and spawn research over the telemetry DB.

Read-only: all writing happens through SqliteRecorder in the runners.
Replay renders boards purely from stored hex boards (no ai/sim imports).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from bot2048.analysis import (
    CHI2_CRIT_P01,
    CLASSIC_SPAWN_PROBS,
    DIRECTION_GLYPHS,
    chi_square_vs_model,
    max_tile_histogram,
    render_board,
    score_stats,
)
from bot2048.core.types import BOARD_SIZE, Board
from bot2048.storage.errors import StorageError
from bot2048.storage.latency import ExperimentLatency, collect_latency, open_readonly
from bot2048.storage.repository import GameRecord, GameRepository, SpawnRow

app = typer.Typer(
    no_args_is_help=True,
    help="Telemetry database: reports, game replay, spawn research.",
)
console = Console()

_DEFAULT_DB = Path("data/games.db")

DbOpt = Annotated[Path, typer.Option("--db", help="Path to the telemetry SQLite database.")]
ExpOpt = Annotated[
    str | None, typer.Option("--experiment", "-e", help="Filter to a single experiment id.")
]


def _open_repository(db: Path) -> GameRepository:
    try:
        return GameRepository(db)
    except StorageError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command()
def report(db: DbOpt = _DEFAULT_DB, experiment: ExpOpt = None) -> None:
    """Experiments overview and per-experiment score/move statistics."""
    with _open_repository(db) as repo:
        experiments = repo.list_experiments()
        if experiment is not None:
            experiments = [e for e in experiments if e.experiment_id == experiment]
            if not experiments:
                console.print(f"[red]experiment {experiment!r} not found in {db}[/red]")
                raise typer.Exit(code=1)
        if not experiments:
            console.print(f"[yellow]no experiments recorded in {db}[/yellow]")
            return

        overview = Table(title="Experiments")
        for column in ("experiment", "created", "kind", "strategy", "games", "notes"):
            overview.add_column(column)

        stats_table = Table(title="Per-experiment game stats")
        stats_table.add_column("experiment")
        for column in (
            "games",
            "finished",
            "mean score",
            "median score",
            "max score",
            "avg moves",
            "avg eval ms",
        ):
            stats_table.add_column(column, justify="right")
        stats_table.add_column("max-tile histogram")

        for exp in experiments:
            games = repo.list_games(exp.experiment_id)
            created = datetime.fromtimestamp(exp.created_at).strftime("%Y-%m-%d %H:%M:%S")
            overview.add_row(
                exp.experiment_id, created, exp.kind, exp.strategy, str(len(games)), exp.notes
            )
            stats = score_stats(games)
            histogram = max_tile_histogram(games)
            hist_text = (
                "  ".join(f"{2**exp_}x{n}" for exp_, n in sorted(histogram.items())) or "-"
            )
            avg_eval = repo.avg_eval_ms(exp.experiment_id)
            avg_eval_text = f"{avg_eval:.2f}" if avg_eval is not None else "-"
            if stats is None:
                stats_table.add_row(
                    exp.experiment_id, str(len(games)), "0", "-", "-", "-", "-",
                    avg_eval_text, hist_text,
                )
            else:
                stats_table.add_row(
                    exp.experiment_id,
                    str(stats.games),
                    str(stats.finished),
                    f"{stats.mean_score:.1f}",
                    f"{stats.median_score:.1f}",
                    str(stats.max_score),
                    f"{stats.avg_moves:.1f}",
                    avg_eval_text,
                    hist_text,
                )

        console.print(overview)
        console.print(stats_table)


@app.command()
def replay(
    game_id: Annotated[str, typer.Argument(help="Game id to replay.")],
    db: DbOpt = _DEFAULT_DB,
    tail: Annotated[
        int | None, typer.Option("--tail", min=1, help="Show only the last N plies.")
    ] = None,
) -> None:
    """Step-by-step ASCII replay of a stored game (every move and spawn)."""
    with _open_repository(db) as repo:
        try:
            record = repo.load_game(game_id)
        except StorageError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

    summary = record.summary
    max_tile = 2**summary.max_exponent if summary.max_exponent is not None else None
    console.print(
        f"[bold]game {summary.game_id}[/bold]  experiment={summary.experiment_id}  "
        f"seed={summary.seed}"
    )
    console.print(
        f"score={summary.final_score}  max_tile={max_tile}  moves={summary.num_moves}  "
        f"end={summary.end_reason}  duration_s={summary.duration_s}"
    )

    initial = _initial_board(record)
    if initial is not None:
        console.print("\ninitial board:")
        console.print(render_board(initial))

    spawn_by_ply = {spawn.ply: spawn for spawn in record.spawns if spawn.ply >= 0}
    moves = record.moves if tail is None else record.moves[-tail:]
    for move in moves:
        glyph = DIRECTION_GLYPHS[move.direction]
        console.print(
            f"\nply {move.ply}: {move.direction.name} {glyph}  +{move.score_gained} "
            f"-> total {move.score_total}  ({move.eval_ms:.2f} ms)"
        )
        console.print(render_board(move.board_after))
        spawn = spawn_by_ply.get(move.ply)
        if spawn is not None:
            row, col = divmod(spawn.cell, BOARD_SIZE)
            console.print(f"spawn {2**spawn.exponent} at r{row}c{col}:")
            console.print(render_board(spawn.board_after))
    if not record.moves:
        console.print("[yellow]game has no recorded moves[/yellow]")


@app.command()
def latency(db: DbOpt = _DEFAULT_DB, experiment: ExpOpt = None) -> None:
    """Per-phase move latency: p50/p90/p95/max, critical share, weak-spot verdict."""
    try:
        conn = open_readonly(db)
    except StorageError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    try:
        reports = collect_latency(conn, experiment)
    except StorageError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    if not reports:
        target = f"experiment {experiment!r}" if experiment else "database"
        console.print(f"[yellow]no timed moves recorded for {target} in {db}[/yellow]")
        return

    for report in reports:
        _print_latency(report)


def _print_latency(report: ExperimentLatency) -> None:
    """Render one experiment's latency report (tables + verdict line)."""
    hit_text = (
        f"{report.prefetch_hit_pct:.0f}%"
        if report.prefetch_hit_pct is not None
        else "unknown"
    )
    capture_text = report.capture_mode if report.capture_mode is not None else "-"
    # Prefer the FACTUAL capture kind (docs/research/telemetry_review.md M3): the
    # configured label stays first (so ``capture=<config>`` is always present),
    # but a silent scrcpy->screencap fallback is now spelled out.
    if report.capture_kind_known and report.capture_screencap_pct is not None:
        capture_text = (
            f"{capture_text} (actual: {report.capture_screencap_pct:.0f}% screencap "
            f"of {report.capture_kind_known} moves)"
        )
    table = Table(
        title=(
            f"Move latency by phase — experiment [bold]{report.experiment_id}[/] "
            f"({report.moves} moves, capture={capture_text})"
        ),
        caption=(
            f"critical path: avg {report.avg_critical_ms:.0f} ms of "
            f"{report.avg_full_ms:.0f} ms full "
            f"({report.avg_critical_ms / report.avg_full_ms * 100.0:.1f}%); "
            f"prefetch hit-rate: {hit_text}"
        ),
    )
    table.add_column("phase")
    for column in ("p50", "p90", "p95", "max"):
        table.add_column(column + " ms", justify="right")

    for row in report.percentiles:
        style = None
        name = row.phase
        if row.phase == report.bottleneck:
            style = "bold red"
            name = f"{row.phase}  <== слабина"
        elif row.phase in ("TOTAL", "CRITICAL"):
            style = "bold"
        table.add_row(
            name,
            f"{row.p50:.1f}",
            f"{row.p90:.1f}",
            f"{row.p95:.1f}",
            f"{row.max_ms:.1f}",
            style=style,
        )
    console.print(table)

    # Predict-gate health (docs/research/telemetry_review.md M2): hit% is the #1
    # early-spawn metric; without it the "settle" phase mixes gate-hits and
    # legacy stable-frame settles. Printed only when the gate actually ran.
    if report.gate_hit_pct is not None:
        gate_line = f"  predict-gate: hit {report.gate_hit_pct:.0f}%"
        if report.gate_fallback_pct is not None:
            gate_line += f", fallback {report.gate_fallback_pct:.0f}%"
        console.print(gate_line)

    # APK mailbox dwell (the drain's own metric): how long a verified packet
    # sat in the host mailbox before the runner consumed it. Only printed when
    # some move actually carried ``apk_age_ms`` — older experiments (or pure
    # frame-path runs) have no such moves and stay silent.
    if report.apk_moves and report.apk_age_avg_ms is not None:
        console.print(
            f"  apk packet-age: avg {report.apk_age_avg_ms:.1f} ms in mailbox "
            f"({report.apk_moves} moves)"
        )

    # Loop gap (design-strategy-logging §5.1): what the phase timer never bills
    # between two commits -- the recorder/log tail, the vision snapshot, loop
    # overhead. The before/after yardstick for the async recorder; silent for
    # experiments recorded before the ``t_commit`` stamp existed.
    for label, gap in (
        ("unaccounted (gap - critical)", report.unaccounted),
        ("prev_commit (persist section)", report.prev_commit),
    ):
        if gap is not None:
            console.print(
                f"  loop gap, {label}: p50 {gap.p50:.1f} / p95 {gap.p95:.1f} / "
                f"p99 {gap.p99:.1f} / max {gap.max_ms:.1f} ms (n={gap.n})"
            )

    top = report.contributions[0]
    runner_up = report.contributions[1] if len(report.contributions) > 1 else None
    verdict = (
        f"  verdict: [bold red]слабина = {top.phase}[/] "
        f"(avg {top.avg_ms:.0f} ms, {top.share_pct:.0f}% of the critical path)"
    )
    if runner_up is not None:
        verdict += (
            f"; next {runner_up.phase} "
            f"(avg {runner_up.avg_ms:.0f} ms, {runner_up.share_pct:.0f}%)"
        )
    console.print(verdict + "\n")


@app.command(name="spawn-stats")
def spawn_stats(
    db: DbOpt = _DEFAULT_DB,
    experiment: ExpOpt = None,
    include_initial: Annotated[
        bool,
        typer.Option(
            "--include-initial/--moves-only",
            help="Count the two initial tiles (ply -1/-2) or only in-game spawns.",
        ),
    ] = True,
) -> None:
    """Empirical spawn distribution vs the classic 90/10 model."""
    with _open_repository(db) as repo:
        stats = repo.spawn_stats(experiment, include_initial=include_initial)

    if stats.total == 0:
        console.print(f"[yellow]no matching spawns recorded in {db}[/yellow]")
        return

    classic = dict(CLASSIC_SPAWN_PROBS)
    exponent_table = Table(title=f"Spawn distribution by exponent (n={stats.total})")
    for column in ("exponent", "tile", "count", "observed %", "classic %"):
        exponent_table.add_column(column, justify="right")
    for exponent in sorted(set(stats.by_exponent) | set(classic)):
        count = stats.by_exponent.get(exponent, 0)
        classic_p = classic.get(exponent)
        exponent_table.add_row(
            str(exponent),
            str(2**exponent),
            str(count),
            f"{100.0 * count / stats.total:.2f}",
            f"{100.0 * classic_p:.2f}" if classic_p is not None else "-",
        )
    console.print(exponent_table)

    chi = chi_square_vs_model(stats.by_exponent)
    if chi is not None:
        critical = CHI2_CRIT_P01.get(chi.degrees_of_freedom)
        verdict = ""
        if critical is not None:
            verdict = (
                "  -> [red]deviates from classic 90/10 (p < 0.01)[/red]"
                if chi.statistic > critical
                else "  -> [green]consistent with classic 90/10 (p >= 0.01)[/green]"
            )
        console.print(
            f"chi-square vs classic: {chi.statistic:.3f} "
            f"(df={chi.degrees_of_freedom}, crit@p0.01={critical}){verdict}"
        )

    heatmap = Table(title="Spawns per cell: count (share %)")
    heatmap.add_column("")
    for col in range(BOARD_SIZE):
        heatmap.add_column(f"c{col}", justify="right")
    for row in range(BOARD_SIZE):
        cells = [f"r{row}"]
        for col in range(BOARD_SIZE):
            n = stats.by_cell.get(row * BOARD_SIZE + col, 0)
            cells.append(f"{n} ({100.0 * n / stats.total:.1f}%)")
        heatmap.add_row(*cells)
    console.print(heatmap)


def _initial_board(record: GameRecord) -> Board | None:
    """Reconstruct the starting board from stored data alone."""
    if record.moves:
        return record.moves[0].board_before
    initial_spawns: list[SpawnRow] = [spawn for spawn in record.spawns if spawn.ply < 0]
    if not initial_spawns:
        return None
    # The later of the two initial spawns has the fuller board.
    fullest = max(initial_spawns, key=lambda spawn: sum(1 for e in spawn.board_after if e))
    return fullest.board_after

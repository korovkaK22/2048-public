"""`bot2048 sim` — run simulated games and benchmark strategies.

The SQLite recorder lives in the (parallel-developed) storage module; if it
is not importable yet, we degrade to an in-memory ListRecorder and warn.
"""

from __future__ import annotations

import enum
import random
import statistics
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from bot2048.ai.expectimax import ExpectimaxStrategy
from bot2048.ai.go_engine import GoExpectimaxStrategy
from bot2048.ai.strategies import CornerStrategy, GreedyStrategy, RandomStrategy, Strategy
from bot2048.core.types import SpawnModel
from bot2048.recording import ListRecorder, Recorder
from bot2048.sim.runner import GameSummary, play_games

app = typer.Typer(no_args_is_help=True, help="Simulation lab: play games headlessly.")
_console = Console()

__all__ = ["app"]


class StrategyName(enum.StrEnum):
    RANDOM = "random"
    GREEDY = "greedy"
    CORNER = "corner"
    EXPECTIMAX = "expectimax"
    GO_EXPECTIMAX = "go-expectimax"


def _make_strategy(
    name: StrategyName,
    depth: int,
    seed: int,
    spawn_model: SpawnModel,
    *,
    farm: bool = False,
    adaptive: bool = True,
    engine_bin: Path | None = None,
) -> Strategy:
    if name is StrategyName.GO_EXPECTIMAX:
        # ``p4`` mirrors the Python-side spawn model so parity holds.
        p4 = dict(spawn_model.exponent_probs).get(2, 0.0)
        return GoExpectimaxStrategy(
            depth=depth,
            p4=p4,
            adaptive=adaptive,
            binary=engine_bin,
            farm=farm,
        )
    if farm:
        _console.print(
            "[yellow]note:[/yellow] --farm only applies to the 'go-expectimax' strategy; ignored."
        )
    if name is StrategyName.RANDOM:
        return RandomStrategy(random.Random(seed))
    if name is StrategyName.GREEDY:
        return GreedyStrategy()
    if name is StrategyName.CORNER:
        return CornerStrategy()
    return ExpectimaxStrategy(depth=depth, spawn_model=spawn_model)


def _close_strategy(strat: Strategy) -> None:
    """Shut down strategies that own external resources (Go subprocess)."""
    closer = getattr(strat, "close", None)
    if callable(closer):
        closer()


def _tile_to_exponent(tile: int | None) -> int | None:
    """Map a tile value (e.g. 2048) to its exponent (11); validate power of two."""
    if tile is None:
        return None
    if tile & (tile - 1) != 0:
        raise typer.BadParameter(f"--stop-tile must be a power of two, got {tile}")
    return tile.bit_length() - 1


def _open_recorder(
    db: Path,
    experiment_id: str,
    strategy_name: str,
    params: dict[str, Any],
) -> Recorder:
    """SqliteRecorder if the storage module exists, else ListRecorder + warning."""
    try:
        from bot2048.storage import ExperimentMeta, SqliteRecorder
    except ImportError:
        _console.print(
            "[yellow]warning:[/yellow] bot2048.storage is not available yet — "
            "recording to memory only (results will NOT be persisted)."
        )
        return ListRecorder()
    db.parent.mkdir(parents=True, exist_ok=True)
    meta = ExperimentMeta(
        experiment_id=experiment_id,
        kind="sim",
        strategy=strategy_name,
        params=params,
    )
    return SqliteRecorder(db, meta)


def _summary_table(title: str, rows: list[tuple[str, list[GameSummary], float]]) -> Table:
    table = Table(title=title)
    table.add_column("strategy")
    table.add_column("games", justify="right")
    table.add_column("mean score", justify="right")
    table.add_column("median score", justify="right")
    table.add_column("max tile histogram")
    table.add_column("moves/s", justify="right")
    for name, summaries, wall_s in rows:
        scores = [s.final_score for s in summaries]
        hist = Counter(2**s.max_exponent for s in summaries)
        hist_text = "  ".join(f"{tile}:{n}" for tile, n in sorted(hist.items()))
        total_moves = sum(s.num_moves for s in summaries)
        table.add_row(
            name,
            str(len(summaries)),
            f"{statistics.mean(scores):,.0f}",
            f"{statistics.median(scores):,.0f}",
            hist_text,
            f"{total_moves / wall_s:,.0f}" if wall_s > 0 else "-",
        )
    return table


@app.command()
def run(
    strategy: Annotated[StrategyName, typer.Option("--strategy")] = StrategyName.EXPECTIMAX,
    depth: Annotated[int, typer.Option("--depth", min=1, help="Expectimax search depth.")] = 3,
    games: Annotated[int, typer.Option("--games", min=1)] = 10,
    seed: Annotated[int, typer.Option("--seed", help="Base seed; game i uses seed+i.")] = 42,
    db: Annotated[Path, typer.Option("--db")] = Path("data/games.db"),
    max_moves: Annotated[int | None, typer.Option("--max-moves", min=1)] = None,
    farm: Annotated[
        bool,
        typer.Option(
            "--farm/--no-farm",
            help="Monobank stop-rule objective: never build 2048 while an "
            "escape exists; a forced 2048 cashes out (go-expectimax only).",
        ),
    ] = False,
    adaptive: Annotated[
        bool,
        typer.Option(
            "--adaptive/--no-adaptive",
            help="Let the Go engine deepen/shallow the search adaptively (go-expectimax only).",
        ),
    ] = True,
    engine_bin: Annotated[
        Path | None,
        typer.Option(
            "--engine-bin",
            help="Explicit path to the Go engine binary (go-expectimax only); "
            f"falls back to ${'{'}BOT2048_ENGINE2048_BIN{'}'} or the default build.",
        ),
    ] = None,
    stop_tile: Annotated[
        int | None,
        typer.Option(
            "--stop-tile",
            min=4,
            help="End the game the moment a tile of this value is created "
            "(monobank rule: 2048 pops the game-over dialog). Power of two.",
        ),
    ] = None,
) -> None:
    """Play N games with one strategy and record them."""
    stop_exponent = _tile_to_exponent(stop_tile)
    spawn_model = SpawnModel()
    strat = _make_strategy(
        strategy, depth, seed, spawn_model, farm=farm, adaptive=adaptive, engine_bin=engine_bin
    )
    experiment_id = uuid.uuid4().hex
    params: dict[str, Any] = {
        "depth": depth,
        "seed": seed,
        "max_moves": max_moves,
        "farm": farm,
        "adaptive": adaptive,
        "engine_bin": str(engine_bin) if engine_bin is not None else None,
        "stop_tile": stop_tile,
    }
    recorder = _open_recorder(db, experiment_id, strat.name, params)
    try:
        started = time.perf_counter()
        summaries = play_games(
            strat,
            spawn_model,
            seeds=[seed + i for i in range(games)],
            recorder=recorder,
            max_moves=max_moves,
            experiment_id=experiment_id,
            params=params,
            stop_exponent=stop_exponent,
        )
        wall_s = time.perf_counter() - started
    finally:
        recorder.close()
        _close_strategy(strat)
    _console.print(_summary_table(f"sim run — {strat.name}", [(strat.name, summaries, wall_s)]))
    _console.print(f"experiment_id: [bold]{experiment_id}[/bold]")


@app.command()
def bench(
    games: Annotated[int, typer.Option("--games", min=1, help="Games per strategy.")] = 10,
    depth: Annotated[int, typer.Option("--depth", min=1)] = 3,
    seed: Annotated[int, typer.Option("--seed")] = 42,
    db: Annotated[Path, typer.Option("--db")] = Path("data/games.db"),
    max_moves: Annotated[int | None, typer.Option("--max-moves", min=1)] = None,
    farm: Annotated[
        bool,
        typer.Option(
            "--farm/--no-farm",
            help="Monobank stop-rule objective for go-expectimax (other strategies ignore it).",
        ),
    ] = False,
    engine_bin: Annotated[
        Path | None,
        typer.Option("--engine-bin", help="Explicit Go engine binary (go-expectimax only)."),
    ] = None,
) -> None:
    """Benchmark ALL strategies on the same seeds and compare."""
    spawn_model = SpawnModel()
    rows: list[tuple[str, list[GameSummary], float]] = []
    for name in StrategyName:
        strat = _make_strategy(
            name,
            depth,
            seed,
            spawn_model,
            farm=farm if name is StrategyName.GO_EXPECTIMAX else False,
            engine_bin=engine_bin,
        )
        experiment_id = uuid.uuid4().hex
        params: dict[str, Any] = {"depth": depth, "seed": seed, "bench": True, "farm": farm}
        recorder = _open_recorder(db, experiment_id, strat.name, params)
        try:
            started = time.perf_counter()
            summaries = play_games(
                strat,
                spawn_model,
                seeds=[seed + i for i in range(games)],
                recorder=recorder,
                max_moves=max_moves,
                experiment_id=experiment_id,
                params=params,
            )
            rows.append((strat.name, summaries, time.perf_counter() - started))
        finally:
            recorder.close()
            _close_strategy(strat)
    _console.print(_summary_table(f"sim bench — {games} games/strategy", rows))


if __name__ == "__main__":  # pragma: no cover - thin launcher
    app()

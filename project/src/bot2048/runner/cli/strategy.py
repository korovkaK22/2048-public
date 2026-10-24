"""``--strategy`` name -> :class:`~bot2048.ai.strategies.Strategy` for the live CLI."""

from __future__ import annotations

import typer

from bot2048.ai import CornerStrategy, ExpectimaxStrategy
from bot2048.ai.go_engine import GoEngineError, GoExpectimaxStrategy
from bot2048.ai.strategies import Strategy
from bot2048.runner.cli.common import err_console
from bot2048.runner.research import make_policy

_STRATEGY_NAMES = ("expectimax", "go", "greedy", "corner", "random")


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

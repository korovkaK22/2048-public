"""Smoke tests for the `bot2048 sim` typer app."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bot2048.sim.cli import _tile_to_exponent, app

# Wide virtual terminal so rich tables don't wrap cell text mid-word.
runner = CliRunner(env={"COLUMNS": "200", "LINES": "60"})


def test_run_random_smoke(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--strategy",
            "random",
            "--games",
            "2",
            "--seed",
            "1",
            "--max-moves",
            "40",
            "--db",
            str(tmp_path / "games.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "random" in result.output
    assert "experiment_id" in result.output


def test_run_expectimax_smoke(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--strategy",
            "expectimax",
            "--depth",
            "1",
            "--games",
            "1",
            "--seed",
            "2",
            "--max-moves",
            "30",
            "--db",
            str(tmp_path / "games.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "expectimax(depth=1)" in result.output


def test_tile_to_exponent() -> None:
    assert _tile_to_exponent(None) is None
    assert _tile_to_exponent(2048) == 11
    assert _tile_to_exponent(1024) == 10
    assert _tile_to_exponent(4) == 2
    with pytest.raises(Exception, match="power of two"):
        _tile_to_exponent(2000)


def test_run_go_stop_tile_rejects_non_power_of_two(tmp_path: Path) -> None:
    """--stop-tile validates its argument before touching the Go engine."""
    result = runner.invoke(
        app,
        [
            "run",
            "--strategy",
            "go-expectimax",
            "--stop-tile",
            "1500",
            "--games",
            "1",
            "--db",
            str(tmp_path / "games.db"),
        ],
    )
    assert result.exit_code != 0
    assert "power of two" in result.output


def test_bench_smoke(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "bench",
            "--games",
            "1",
            "--depth",
            "1",
            "--seed",
            "3",
            "--max-moves",
            "25",
            "--db",
            str(tmp_path / "games.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    for name in ("random", "greedy", "corner", "expectimax(depth=1)"):
        assert name in result.output

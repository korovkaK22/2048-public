"""CLI smoke tests: report / replay / spawn-stats against a synthetic db."""

from __future__ import annotations

from pathlib import Path

from conftest import GAME_ID
from typer.testing import CliRunner

from bot2048.storage.cli import app

runner = CliRunner()


def test_report(synthetic_db: Path) -> None:
    result = runner.invoke(app, ["report", "--db", str(synthetic_db)])
    assert result.exit_code == 0, result.output
    assert "exp-1" in result.output
    assert "greedy" in result.output
    assert "4x1" in result.output  # max-tile histogram: one game topping out at tile 4


def test_report_unknown_experiment(synthetic_db: Path) -> None:
    result = runner.invoke(
        app, ["report", "--db", str(synthetic_db), "--experiment", "nope"]
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_report_missing_db(tmp_path: Path) -> None:
    result = runner.invoke(app, ["report", "--db", str(tmp_path / "absent.db")])
    assert result.exit_code == 1


def test_replay(synthetic_db: Path) -> None:
    result = runner.invoke(app, ["replay", GAME_ID, "--db", str(synthetic_db)])
    assert result.exit_code == 0, result.output
    assert "initial board" in result.output
    assert "ply 0: LEFT" in result.output
    assert "ply 1: UP" in result.output
    assert "ply 2: DOWN" in result.output
    assert "spawn 4 at r3c3" in result.output
    assert result.output.count("+------+") > 0


def test_replay_tail(synthetic_db: Path) -> None:
    result = runner.invoke(
        app, ["replay", GAME_ID, "--db", str(synthetic_db), "--tail", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "ply 0: LEFT" not in result.output
    assert "ply 2: DOWN" in result.output


def test_replay_unknown_game(synthetic_db: Path) -> None:
    result = runner.invoke(app, ["replay", "no-such-game", "--db", str(synthetic_db)])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_spawn_stats(synthetic_db: Path) -> None:
    result = runner.invoke(app, ["spawn-stats", "--db", str(synthetic_db)])
    assert result.exit_code == 0, result.output
    assert "chi-square vs classic" in result.output
    assert "r0" in result.output
    assert "c3" in result.output


def test_spawn_stats_moves_only(synthetic_db: Path) -> None:
    result = runner.invoke(
        app, ["spawn-stats", "--db", str(synthetic_db), "--moves-only"]
    )
    assert result.exit_code == 0, result.output
    assert "n=3" in result.output

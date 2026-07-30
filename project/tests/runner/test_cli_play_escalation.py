"""``play`` -> LiveConfig wiring for the ``--escalate-*`` budget flags.

Invokes the real typer command with every device/vision seam stubbed out
(no adb, no calibration file, no lifecycle guard) and captures the
:class:`LiveConfig` the command constructs, so the tests would fail loudly
if the CLI options stopped reaching the config.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

import bot2048.runner.cli as cli
from bot2048.runner.live import LiveConfig

runner = CliRunner()

_BASE_ARGS = ["play", "--strategy", "expectimax", "--capture", "screencap"]


class _StubVision:
    """Only attribute access happens before ``_run_guarded`` (stubbed)."""

    detect = staticmethod(lambda frame: None)
    read = staticmethod(lambda frame: None)
    read_cells = staticmethod(lambda frame: None)
    learn_tile = staticmethod(lambda cell, exponent, frame: None)
    calibration = None


class _StubDevice:
    """Stored by ``play``; never used because the guard is stubbed."""

    def connect(self) -> None:  # pragma: no cover - never called
        raise AssertionError("stub device must not connect")


@pytest.fixture()
def captured_configs(monkeypatch: pytest.MonkeyPatch) -> list[LiveConfig]:
    """Run ``play`` for real but record every LiveConfig it builds."""
    configs: list[LiveConfig] = []
    real_config = cli.LiveConfig

    def record(**kwargs: Any) -> LiveConfig:
        config = real_config(**kwargs)
        configs.append(config)
        return config

    monkeypatch.setattr(cli, "LiveConfig", record)
    monkeypatch.setattr(cli, "_make_vision", lambda path: _StubVision())
    monkeypatch.setattr(cli, "_connect", lambda serial: _StubDevice())
    monkeypatch.setattr(cli, "_run_guarded", lambda **kwargs: None)
    return configs


def test_escalate_flags_reach_live_config(captured_configs: list[LiveConfig]) -> None:
    result = runner.invoke(
        cli.app,
        [
            *_BASE_ARGS,
            "--budget-ms",
            "150",
            "--escalate-after-ply",
            "120",
            "--escalate-when-empty-le",
            "4",
            "--escalate-budget-ms",
            "800",
        ],
    )
    assert result.exit_code == 0, result.output
    (config,) = captured_configs
    assert config.escalate_after_ply == 120
    assert config.escalate_when_empty_le == 4
    assert config.escalate_budget_ms == 800.0


def test_omitting_flags_keeps_escalation_off(
    captured_configs: list[LiveConfig],
) -> None:
    result = runner.invoke(cli.app, _BASE_ARGS)
    assert result.exit_code == 0, result.output
    (config,) = captured_configs
    assert config.escalate_after_ply is None
    assert config.escalate_when_empty_le is None
    assert config.escalate_budget_ms is None
    # The 3-tier ladder knobs are off too (base budget throughout).
    assert config.late_after_ply is None
    assert config.late_budget_ms is None
    assert config.endgame_when_empty_le is None
    assert config.endgame_budget_ms is None
    assert config.endgame_after_ply is None


def test_ladder_flags_reach_live_config(captured_configs: list[LiveConfig]) -> None:
    result = runner.invoke(
        cli.app,
        [
            *_BASE_ARGS,
            "--budget-ms",
            "250",
            "--late-after-ply",
            "2067",
            "--late-budget-ms",
            "2000",
            "--endgame-when-empty-le",
            "3",
            "--endgame-budget-ms",
            "9000",
            "--endgame-after-ply",
            "2100",
        ],
    )
    assert result.exit_code == 0, result.output
    (config,) = captured_configs
    assert config.late_after_ply == 2067
    assert config.late_budget_ms == 2000.0
    assert config.endgame_when_empty_le == 3
    assert config.endgame_budget_ms == 9000.0
    assert config.endgame_after_ply == 2100

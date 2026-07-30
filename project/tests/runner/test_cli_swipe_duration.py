"""``play`` -> LiveConfig wiring for the live ``--swipe-duration`` default.

Invokes the real typer command with every device/vision seam stubbed out
(same pattern as test_cli_play_escalation.py) and captures the
:class:`LiveConfig` the command constructs, so these tests fail loudly if
the 45 ms live default stops reaching the config or stops yielding to an
explicit flag / ``BOT2048_SWIPE_MS``.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

import bot2048.runner.cli as cli
from bot2048.device.humanizer import SWIPE_MS_ENV_VAR
from bot2048.runner.live import LiveConfig
from bot2048.runner.live_config import LIVE_SWIPE_DURATION_MS

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
    monkeypatch.delenv(SWIPE_MS_ENV_VAR, raising=False)
    return configs


def test_unset_flag_resolves_to_live_default(
    captured_configs: list[LiveConfig],
) -> None:
    result = runner.invoke(cli.app, _BASE_ARGS)
    assert result.exit_code == 0, result.output
    assert captured_configs[0].swipe_duration_ms == LIVE_SWIPE_DURATION_MS


def test_explicit_flag_wins_over_live_default(
    captured_configs: list[LiveConfig],
) -> None:
    result = runner.invoke(cli.app, [*_BASE_ARGS, "--swipe-duration", "80"])
    assert result.exit_code == 0, result.output
    assert captured_configs[0].swipe_duration_ms == 80


def test_env_var_suppresses_live_default(
    captured_configs: list[LiveConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With BOT2048_SWIPE_MS set the CLI must leave swipe_duration_ms unset so
    # the humanizer's env-profile path keeps honoring the operator override.
    monkeypatch.setenv(SWIPE_MS_ENV_VAR, "60")
    result = runner.invoke(cli.app, _BASE_ARGS)
    assert result.exit_code == 0, result.output
    assert captured_configs[0].swipe_duration_ms is None


def test_live_default_keeps_margin_over_game_floor() -> None:
    # research/swipe_throughput.md: the game registers gestures down to
    # ~20 ms; the live default must keep a comfortable margin above that
    # while staying well under the 80 ms fast profile.
    assert LIVE_SWIPE_DURATION_MS == 45
    assert LIVE_SWIPE_DURATION_MS >= 2 * 20
    assert LIVE_SWIPE_DURATION_MS < 80

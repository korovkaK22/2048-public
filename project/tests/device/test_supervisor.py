"""Supervisor recovery FSM: transient tolerance vs fail-closed FATAL.

Regression for the 25k live game that FATAL'd on a single transient
``not_in_game`` frame (the game-over card sliding in dropped the board match
to 69% < 0.75 -> NOT_IN_GAME -> immediate FATAL). One bad frame mid-game must
now be re-polled away; a persistently stuck non-board must still FATAL with no
blind taps (banking app doctrine).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from bot2048.device.supervisor import (
    FatalIncident,
    Supervisor,
    SupervisorConfig,
)
from bot2048.runner.alerts import AlertCode, AlertWriter
from bot2048.runner.escalation import ProbeResult
from bot2048.runner.incidents import Image
from bot2048.vision.states import ScreenState, StateDetection

_FRAME: Image = np.zeros((4, 4, 3), dtype=np.uint8)


def _probe(state: ScreenState, frac: float) -> ProbeResult:
    """A ProbeResult with a real image (screencap worked, board may not)."""
    return ProbeResult(
        state=state,
        image=_FRAME,
        uidump_xml=None,
        board_match_frac=frac,
        reason=f"test {state.value}",
    )


def _det(state: ScreenState, frac: float) -> StateDetection:
    return StateDetection(state=state, reason=f"test {state.value}", board_match_frac=frac)


class _FakeDevice:
    """Observation/action surface; records taps & wakes to prove no blind taps."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.wakes = 0
        self.classify_calls = 0

    def screenshot(self) -> Image:
        return _FRAME

    def ui_dump(self) -> str:  # pragma: no cover - screencap never blinds here
        return ""

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def wake(self) -> None:
        self.wakes += 1


def _classifier(
    dev: _FakeDevice, scripted: list[StateDetection]
) -> Callable[[Image], StateDetection]:
    it: Iterator[StateDetection] = iter(scripted)

    def classify(_image: Image) -> StateDetection:
        dev.classify_calls += 1
        return next(it)

    return classify


def _make(
    tmp_path: Path, scripted_repolls: list[StateDetection]
) -> tuple[Supervisor, _FakeDevice, Path]:
    dev = _FakeDevice()
    alerts_path = tmp_path / "alerts.jsonl"
    sup = Supervisor(
        device=dev,
        classify=_classifier(dev, scripted_repolls),
        alerts=AlertWriter(alerts_path),
        config=SupervisorConfig(incidents_root=tmp_path / "incidents"),
        run_id="test-run",
        sleep=lambda _s: None,
        clock=lambda: 0.0,
    )
    return sup, dev, alerts_path


def _alerts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("bad", [ScreenState.NOT_IN_GAME, ScreenState.UNKNOWN])
def test_single_transient_frame_recovers_without_fatal(
    tmp_path: Path, bad: ScreenState
) -> None:
    """One bad frame between two GAME frames: the first re-poll recovers."""
    sup, dev, alerts_path = _make(tmp_path, [_det(ScreenState.GAME, 1.0)])

    image = sup.handle_error(
        AlertCode.SCREEN_UNEXPECTED,
        f"expected GAME, got {bad.value}: board only 69%",
        probe=_probe(bad, 0.6875),
        game_id="g1",
        move_no=1419,
    )

    assert image is _FRAME  # a healthy GAME frame handed back to the loop
    assert dev.taps == []  # NEVER a blind tap on an uncertain screen
    assert dev.classify_calls == 1  # exactly one re-poll needed
    levels = [a["level"] for a in _alerts(alerts_path)]
    assert levels == ["ERROR"]  # ERROR incident only, NO FATAL


def test_persistent_not_in_game_still_fatals(tmp_path: Path) -> None:
    """A stuck non-board (real game-over popup) exhausts re-polls -> FATAL."""
    # Robust to config.transient_repolls (bumped 2->20 to outlast the cat-mascot
    # paw animation): script exactly that many stuck frames and assert against it.
    repolls = SupervisorConfig(incidents_root=tmp_path / "incidents").transient_repolls
    sup, dev, alerts_path = _make(
        tmp_path,
        [_det(ScreenState.NOT_IN_GAME, 0.6875)] * repolls,  # every re-poll still non-GAME
    )

    with pytest.raises(FatalIncident) as excinfo:
        sup.handle_error(
            AlertCode.SCREEN_UNEXPECTED,
            "expected GAME, got not_in_game: board only 69%",
            probe=_probe(ScreenState.NOT_IN_GAME, 0.6875),
            game_id="g1",
            move_no=1419,
        )

    assert excinfo.value.alert.code is AlertCode.SCREEN_UNEXPECTED
    assert dev.taps == []  # fail-closed: no blind taps, ever
    assert dev.classify_calls == repolls  # config.transient_repolls re-polls attempted
    levels = [a["level"] for a in _alerts(alerts_path)]
    assert levels == ["ERROR", "FATAL"]


def test_recovery_on_a_later_repoll(tmp_path: Path) -> None:
    """The bad streak may span more than one re-poll before GAME returns."""
    sup, dev, _ = _make(
        tmp_path,
        [_det(ScreenState.NOT_IN_GAME, 0.6875), _det(ScreenState.GAME, 1.0)],
    )
    image = sup.handle_error(
        AlertCode.SCREEN_UNEXPECTED,
        "transient",
        probe=_probe(ScreenState.NOT_IN_GAME, 0.6875),
    )
    assert image is _FRAME
    assert dev.classify_calls == 2
    assert dev.taps == []


def test_device_lost_fatals_immediately_without_repoll(tmp_path: Path) -> None:
    """Regression guard: DEVICE_LOST is not a transient board dip -> no re-poll.

    (The outer session loop, not the re-poll, is what recovers a dropped adb.)
    """
    sup, dev, alerts_path = _make(tmp_path, [])  # classifier must never be called
    with pytest.raises(FatalIncident) as excinfo:
        sup.handle_error(
            AlertCode.DEVICE_LOST,
            "screenshot failed",
            probe=_probe(ScreenState.DEVICE_LOST, 0.0),
        )
    assert excinfo.value.alert.code is AlertCode.DEVICE_LOST
    assert dev.classify_calls == 0  # straight to FATAL, no re-poll
    assert dev.taps == []
    assert [a["level"] for a in _alerts(alerts_path)] == ["ERROR", "FATAL"]

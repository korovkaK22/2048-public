"""Regression: a perfect board with a dim «Нова гра» button is GAME.

Incident 20260712T071957Z_SCREEN_UNEXPECTED: a genuine fresh game whose
«Нова гра» button rendered its dimmed ↻ loading variant put the single
``new_game_btn`` anchor pixel on background, so a 100%-matching board
classified UNKNOWN ("board 100% but new_game_btn missing") and FATALed the
live run (no safe recovery action; blind taps forbidden). ``detect_state``
now trusts a near-perfect board (``_BOARD_HIGH_CONF_FRAC``, 0.95 -- with
1/16 cell granularity only a perfect 16/16 board qualifies) even when that
one pixel is dark. Fixture ``game_dim_new_game_btn.png`` is the incident's
``screenshot_after.png`` verbatim.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from bot2048.vision.calibration import Calibration
from bot2048.vision.parser import Image, cell_half_size
from bot2048.vision.states import ScreenState, StateAnchors, detect_state


def test_incident_frame_classifies_game(
    load_image: Callable[[str], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    """The exact live frame that took the run down now classifies GAME."""
    detection = detect_state(load_image("game_dim_new_game_btn.png"), calib, anchors)
    assert detection.state is ScreenState.GAME
    assert detection.board_match_frac == 1.0
    assert "high-conf" in detection.reason


def test_one_bad_cell_stays_below_the_gate(
    paint_board: Callable[[Sequence[int]], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    """15/16 = 0.9375 < 0.95: a single unknown cell still yields UNKNOWN.

    Pins the gate boundary from below: without the new_game_btn pixel, only
    a PERFECT board may classify GAME.
    """
    frame = paint_board((0,) * 16).copy()
    half = cell_half_size(calib)
    cx, cy = calib.cell_centers[0]
    frame[cy - half : cy + half, cx - half : cx + half] = (128, 128, 128)
    detection = detect_state(frame, calib, anchors)
    assert detection.state is ScreenState.UNKNOWN
    assert detection.board_match_frac == pytest.approx(15 / 16)
    assert "new_game_btn missing" in detection.reason

"""Grid detection: locate the board panel in raw frames."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from bot2048.vision.calibration import Calibration, CalibrationError
from bot2048.vision.grid import detect_grid
from tests.vision.conftest import Image


@pytest.mark.parametrize("name", ["board_fresh.png", "board_full.jpg", "live_1.png"])
def test_detected_geometry_matches_reference(
    name: str, load_image: Callable[[str], Image], calib: Calibration
) -> None:
    geometry = detect_grid(load_image(name))
    assert geometry.grid_bbox == calib.grid_bbox
    for (dx, dy), (rx, ry) in zip(geometry.cell_centers, calib.cell_centers, strict=True):
        assert abs(dx - rx) <= 2
        assert abs(dy - ry) <= 2
    assert 150 < geometry.cell_size < 250
    pl, pt, pr, pb = geometry.panel_bbox
    gl, gt, gr, gb = geometry.grid_bbox
    assert pl <= gl < gr <= pr
    assert pt <= gt < gb <= pb


def test_popup_frame_rejected(load_image: Callable[[str], Image]) -> None:
    with pytest.raises(CalibrationError, match="panel not found"):
        detect_grid(load_image("confirm_new_game.jpg"))


def test_blank_frame_rejected() -> None:
    blank = np.full((2400, 1080, 3), 255, dtype=np.uint8)
    with pytest.raises(CalibrationError):
        detect_grid(blank)

"""Shared fixtures for the on-device sampler/packet tests.

Reuses the reference-device calibration and the REAL device screenshots that
already live under ``tests/vision/fixtures`` (1080x2400 frames) -- the whole
point of the accuracy tests is to prove the color-only on-device read
reproduces the host's digits+color parser on real pixels.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import numpy as np
import numpy.typing as npt
import pytest

from bot2048.vision.calibration import Calibration
from bot2048.vision.cli import MissingImageBackendError
from bot2048.vision.parser import cell_half_size
from bot2048.vision.reference import reference_calibration
from tests.imgcache import decode_fixture

Image = npt.NDArray[np.uint8]

#: Real device frames are shared with the vision suite (no duplication).
VISION_FIXTURES = Path(__file__).resolve().parents[1] / "vision" / "fixtures"

#: Lossless PNG game frames only (JPEG recompression perturbs colors; the
#: strict color-vs-digits equality test uses PNGs). Every one is a settled
#: game board on the reference device.
GAME_PNG_FIXTURES: tuple[str, ...] = (
    "board_256.png",
    "board_a.png",
    "board_b.png",
    "board_c.png",
    "board_d_labeled.png",
    "board_fresh.png",
    "live_1.png",
    "live_2.png",
    "live_3.png",
    "state_e.png",
)


@pytest.fixture(scope="session")
def calib() -> Calibration:
    """Reference-device calibration (pure data, no files needed)."""
    return reference_calibration()


@pytest.fixture(scope="session")
def load_image() -> Callable[[str], Image]:
    """Load a shared vision fixture as RGB; skips if no image backend."""

    def _load(name: str) -> Image:
        try:
            return decode_fixture(VISION_FIXTURES / name)
        except MissingImageBackendError as exc:  # pragma: no cover - env-dependent
            pytest.skip(str(exc))

    return _load


@pytest.fixture(scope="session")
def paint_board(calib: Calibration) -> Callable[[Sequence[tuple[int, int, int]]], Image]:
    """Synthetic full frame: each cell filled with a caller-chosen RGB.

    Lets a test paint an off-palette color into a cell to exercise the
    uncertain flag deterministically, without any screenshot file.
    """
    half = cell_half_size(calib) + 10

    def _paint(cell_colors: Sequence[tuple[int, int, int]]) -> Image:
        width, height = calib.screen_size
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        for (cx, cy), rgb in zip(calib.cell_centers, cell_colors, strict=True):
            frame[cy - half : cy + half, cx - half : cx + half] = rgb
        return cast(Image, frame)

    return _paint

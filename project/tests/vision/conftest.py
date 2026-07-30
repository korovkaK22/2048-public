"""Shared fixtures for the vision test suite.

Real-device screenshots live in ``tests/vision/fixtures`` (1080x2400 frames
from the reference device, see docs/device-ui-notes.md). Decoding them needs
cv2 or Pillow; tests that read fixture files skip when neither is installed.
Synthetic-frame tests run on the core dependencies alone.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pytest

from bot2048.vision.calibration import Calibration
from bot2048.vision.cli import MissingImageBackendError
from bot2048.vision.parser import cell_half_size
from bot2048.vision.reference import STATE_ANCHORS_DICT, reference_calibration
from bot2048.vision.states import StateAnchors
from tests.imgcache import decode_fixture

Image = npt.NDArray[np.uint8]

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def calib() -> Calibration:
    """Reference-device calibration (pure data, no files needed)."""
    return reference_calibration()


@pytest.fixture(scope="session")
def anchors() -> StateAnchors:
    """Reference-device state anchors."""
    return StateAnchors.from_dict(cast("dict[str, Any]", STATE_ANCHORS_DICT))


@pytest.fixture(scope="session")
def load_image() -> Callable[[str], Image]:
    """Load a fixture screenshot as RGB; skips if no image backend."""

    def _load(name: str) -> Image:
        try:
            return decode_fixture(FIXTURES / name)
        except MissingImageBackendError as exc:  # pragma: no cover - env-dependent
            pytest.skip(str(exc))

    return _load


@pytest.fixture(scope="session")
def paint_board(calib: Calibration) -> Callable[[Sequence[int]], Image]:
    """Synthetic frame factory: cells painted their palette color, no digits.

    Digit-free tiles exercise the color-fallback path of the parser without
    any screenshot files.
    """
    by_exp: dict[int, tuple[int, int, int]] = {}
    for rgb, exp in calib.palette:
        by_exp.setdefault(exp, rgb)

    def _paint(exponents: Sequence[int]) -> Image:
        width, height = calib.screen_size
        frame = np.full((height, width, 3), 40, dtype=np.uint8)
        half = cell_half_size(calib) + 10
        for (cx, cy), exp in zip(calib.cell_centers, exponents, strict=True):
            frame[cy - half : cy + half, cx - half : cx + half] = by_exp[exp]
        return cast(Image, frame)

    return _paint

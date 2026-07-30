"""Board-panel detection: find the 4x4 grid in a raw screenshot.

Makes ``bot2048 vision calibrate --image`` derive the cell geometry from the
frame itself instead of trusting the hand-measured reference constants, so a
new device/resolution only needs a palette check, not a ruler.

How it works (measured on the reference device, 1080x2400):

The board panel is the darkest large UI element on the game screen -- its
frame, cell gaps and margins sit at luminance ~12-17 while empty cells are
>= 26, the purple background >= 40 and the area below the board dithers
around ~23. Thresholding at :data:`DARK_MAX_LUM` and projecting the dark
fraction onto each axis gives razor-sharp panel edges:

* columns: within the central y-band the panel margin columns are >= 75 %
  dark, everything left/right of the panel is < 15 % dark -> ``left/right``;
* rows: across the panel's x-span the top margin rows are ~100 % dark,
  rows above are ~0 % -> ``top``;
* the panel is square, so ``bottom = top + width`` (the area right below
  the board is too close to the threshold to detect the bottom edge
  reliably -- measured, not an assumption).

Cell centers / grid bbox are then fixed FRACTIONS of the panel square,
measured once on the reference device where the panel is
``(90, 741) .. (989, 1641)`` (900 px) and the known-good geometry is
``COL_CENTERS = (216, 432, 648, 864)``, ``ROW_CENTERS = (868, ...)``,
cell size 189 px, ``GRID_BBOX = (107, 760, 973, 1625)`` (see
:mod:`bot2048.vision.reference`). Detection reproduces all of those within
1 px on every fixture frame (tests/vision/test_grid.py).

The popup-dimmed frame (CONFIRM_NEW_GAME) darkens the whole screen below
the threshold; the sanity checks reject it with :class:`CalibrationError`
instead of returning a garbage grid.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from bot2048.core.types import BOARD_SIZE
from bot2048.vision.calibration import CalibrationError

__all__ = ["DARK_MAX_LUM", "GridGeometry", "detect_grid"]

#: Pixels with mean(R, G, B) below this are "panel-dark" (see module docs).
DARK_MAX_LUM: float = 21.0

#: Minimum dark fraction for a column (within the y-band) / row (within the
#: panel x-span) to count as part of the panel.
DARK_FRAC_MIN: float = 0.5

#: Central y-band (fractions of image height) used for the column profile.
Y_BAND: tuple[float, float] = (0.25, 0.75)

#: Row search range (fractions of image height) for the panel top edge.
TOP_RANGE: tuple[float, float] = (0.05, 0.60)

#: Acceptable panel width as a fraction of the image width (reference:
#: 900/1080 = 0.83). Rejects both noise blobs and the popup-dimmed frame
#: where "dark" covers the whole screen.
PANEL_WIDTH_RANGE: tuple[float, float] = (0.55, 0.95)

# -- panel-square fractions, measured on the reference device ---------------
# Panel (90, 741)..(989, 1641), side 900 px; cells start at 121/774, cell
# size 189 px, pitch 216.33 px; GRID_BBOX (107, 760, 973, 1625).
_CENTER_X0_F: float = 126.0 / 900.0  # first column center - panel left
_CENTER_Y0_F: float = 127.0 / 900.0  # first row center - panel top
_PITCH_F: float = 216.3333 / 900.0  # center-to-center distance
_CELL_F: float = 189.0 / 900.0  # tile face side
_BBOX_LEFT_F: float = 17.0 / 900.0  # GRID_BBOX inset from the panel edges
_BBOX_TOP_F: float = 19.0 / 900.0
_BBOX_RIGHT_F: float = 883.0 / 900.0
_BBOX_BOTTOM_F: float = 884.0 / 900.0


@dataclass(frozen=True, slots=True)
class GridGeometry:
    """Detected board geometry, in the coordinate space of the screenshot."""

    panel_bbox: tuple[int, int, int, int]  #: dark panel (left, top, right, bottom), exclusive
    grid_bbox: tuple[int, int, int, int]  #: cells + half-gap, for swipe geometry
    cell_centers: tuple[tuple[int, int], ...]  #: 16 true centers, row-major
    cell_size: int  #: tile face side in px


def detect_grid(image: npt.NDArray[np.uint8]) -> GridGeometry:
    """Locate the board panel in a game-screen screenshot.

    Raises:
        CalibrationError: no plausible panel found (wrong screen, popup
            dim overlay, not a game frame). Never returns garbage geometry.
    """
    if image.ndim != 3 or image.shape[2] < 3:
        raise CalibrationError(f"expected an (H, W, 3) RGB image, got shape {image.shape!r}")
    height, width = image.shape[0], image.shape[1]
    lum = image[:, :, :3].astype(np.float64).mean(axis=2)
    dark = lum < DARK_MAX_LUM

    # Columns: dark fraction within the central y-band.
    band = dark[int(Y_BAND[0] * height) : int(Y_BAND[1] * height), :]
    col_frac = band.mean(axis=0)
    cols = np.flatnonzero(col_frac > DARK_FRAC_MIN)
    if cols.size == 0:
        raise CalibrationError("board panel not found: no dark columns")
    left, right = int(cols[0]), int(cols[-1]) + 1
    side = right - left
    if not PANEL_WIDTH_RANGE[0] * width <= side <= PANEL_WIDTH_RANGE[1] * width:
        raise CalibrationError(
            f"board panel not found: dark span {left}..{right} ({side} px) is not "
            f"{PANEL_WIDTH_RANGE[0]:.0%}..{PANEL_WIDTH_RANGE[1]:.0%} of the {width} px width "
            "(popup overlay or not a game screen?)"
        )

    # Top edge: first sufficiently dark row across the panel's x-span.
    row_frac = dark[:, left:right].mean(axis=1)
    rows = np.arange(int(TOP_RANGE[0] * height), int(TOP_RANGE[1] * height))
    hits = rows[row_frac[rows] > DARK_FRAC_MIN]
    if hits.size == 0:
        raise CalibrationError("board panel not found: no dark top edge")
    top = int(hits[0])
    bottom = top + side  # the panel is square (see module docs)
    if bottom > height:
        raise CalibrationError(
            f"board panel implausible: square bottom {bottom} exceeds image height {height}"
        )

    centers = tuple(
        (
            left + round(_CENTER_X0_F * side + col * _PITCH_F * side),
            top + round(_CENTER_Y0_F * side + row * _PITCH_F * side),
        )
        for row in range(BOARD_SIZE)
        for col in range(BOARD_SIZE)
    )
    grid_bbox = (
        left + round(_BBOX_LEFT_F * side),
        top + round(_BBOX_TOP_F * side),
        left + round(_BBOX_RIGHT_F * side),
        top + round(_BBOX_BOTTOM_F * side),
    )
    return GridGeometry(
        panel_bbox=(left, top, right, bottom),
        grid_bbox=grid_bbox,
        cell_centers=centers,
        cell_size=round(_CELL_F * side),
    )

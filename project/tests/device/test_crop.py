"""Board-crop geometry + calibration transform (``bot2048.device.crop``).

No device, no adb, no ffmpeg: pure geometry against the reference-device
calibration, plus a parse-equivalence check on the real 1080x2400 fixture
screenshots (skipped when no image backend is installed, same policy as
``tests/vision``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pytest

from bot2048.device.crop import (
    CROP_ENV_VAR,
    CropRegion,
    anchors_outside_region,
    board_crop_from_env,
    crop_calibration,
    crop_enabled,
    crop_frame,
    crop_region_for_board,
)
from bot2048.vision.calibration import Calibration, CalibrationError
from bot2048.vision.glyphs import GlyphStore
from bot2048.vision.parser import DEFAULT_PATCH_RADIUS, cell_half_size, parse_board
from bot2048.vision.reference import STATE_ANCHORS_DICT, reference_calibration
from bot2048.vision.states import StateAnchors

Image = npt.NDArray[np.uint8]

FIXTURES = Path(__file__).parents[1] / "vision" / "fixtures"

#: Fixture frames with a parseable board (see tests/vision/test_parser.py).
BOARD_FIXTURES = ("board_a.png", "board_b.png", "board_c.png", "board_fresh.png", "live_1.png")


@pytest.fixture(scope="session")
def calib() -> Calibration:
    return reference_calibration()


@pytest.fixture(scope="session")
def anchors() -> StateAnchors:
    return StateAnchors.from_dict(cast("dict[str, Any]", STATE_ANCHORS_DICT))


def load_fixture(name: str) -> Image:
    """Fixture PNG as RGB uint8; skips (like tests/vision) without a backend."""
    from bot2048.vision.cli import MissingImageBackendError
    from tests.imgcache import decode_fixture

    try:
        return decode_fixture(FIXTURES / name)
    except MissingImageBackendError:  # pragma: no cover - backend present in CI
        pytest.skip("no image backend (cv2/Pillow) installed")


# -- CropRegion ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("x", "y", "width", "height"),
    [
        (-1, 0, 32, 48),
        (0, -1, 32, 48),
        (0, 0, 0, 48),
        (0, 0, 32, 0),
        (0, 0, 31, 48),  # odd width
        (0, 0, 32, 47),  # odd height
    ],
)
def test_region_rejects_bad_values(x: int, y: int, width: int, height: int) -> None:
    with pytest.raises(ValueError):
        CropRegion(x=x, y=y, width=width, height=height)


def test_region_server_arg_is_width_height_x_y() -> None:
    region = CropRegion(x=88, y=740, width=904, height=904)
    assert region.server_arg == "904:904:88:740"
    assert region.as_config == (88, 740, 904, 904)
    assert region.size == (904, 904)


def test_region_transforms_round_trip() -> None:
    region = CropRegion(x=88, y=740, width=904, height=904)
    assert region.to_crop((216, 868)) == (128, 128)
    assert region.to_full(region.to_crop((864, 1517))) == (864, 1517)
    assert region.contains((216, 868), margin=80)
    assert not region.contains((540, 2143))  # new_game_btn: below the board
    assert not region.contains((92, 744), margin=80)  # inside, but margin pokes out


# -- crop_region_for_board ----------------------------------------------------------


def test_board_region_covers_padded_bbox_aligned(calib: Calibration) -> None:
    region = crop_region_for_board(calib)
    left, top, right, bottom = calib.grid_bbox
    # The padded board is fully inside...
    assert region.x <= left - 16 and region.y <= top - 16
    assert region.x + region.width >= right + 16
    assert region.y + region.height >= bottom + 16
    # ...dimensions are encoder-aligned, offsets even, all inside the screen.
    assert region.width % 8 == 0 and region.height % 8 == 0
    assert region.x % 2 == 0 and region.y % 2 == 0
    screen_w, screen_h = calib.screen_size
    assert region.x + region.width <= screen_w
    assert region.y + region.height <= screen_h
    # Regression pin for the reference device: ~31% of the full-frame pixels.
    assert region == CropRegion(x=88, y=740, width=904, height=904)
    assert region.width * region.height / (screen_w * screen_h) < 0.32


def test_board_region_clamps_at_screen_edges() -> None:
    tight = Calibration(
        grid_bbox=(2, 2, 100, 100),
        cell_centers=tuple((14 + 24 * (i % 4), 14 + 24 * (i // 4)) for i in range(16)),
        palette=(((21, 21, 36), 0),),
        screen_size=(104, 104),
    )
    region = crop_region_for_board(tight, padding=4)
    assert region.x >= 0 and region.y >= 0
    assert region.x + region.width <= 104
    assert region.y + region.height <= 104


def test_board_region_rejects_bad_padding_align(calib: Calibration) -> None:
    with pytest.raises(CalibrationError):
        crop_region_for_board(calib, padding=-1)
    with pytest.raises(CalibrationError):
        crop_region_for_board(calib, align=3)


# -- crop_calibration ---------------------------------------------------------------


def test_crop_calibration_translates_everything(calib: Calibration) -> None:
    region = crop_region_for_board(calib)
    cropped = crop_calibration(calib, region)
    assert cropped.screen_size == region.size
    assert cropped.palette == calib.palette
    assert cropped.cell_centers == tuple(region.to_crop(c) for c in calib.cell_centers)
    left, top, right, bottom = calib.grid_bbox
    assert cropped.grid_bbox == (
        left - region.x,
        top - region.y,
        right - region.x,
        bottom - region.y,
    )
    # The derived cell pitch (and thus the crop half-size) is unchanged.
    assert cell_half_size(cropped) == cell_half_size(calib)


def test_crop_calibration_rejects_region_cutting_cells(calib: Calibration) -> None:
    too_small = CropRegion(x=216, y=868, width=400, height=400)  # cuts columns 0 and 3
    with pytest.raises(CalibrationError, match="outside the crop region"):
        crop_calibration(calib, too_small)


# -- crop_frame + parse equivalence on real fixtures --------------------------------


def test_crop_frame_shape_and_content() -> None:
    image = cast(Image, np.arange(40 * 60 * 3, dtype=np.uint8).reshape(60, 40, 3) % 251)
    region = CropRegion(x=4, y=10, width=16, height=20)
    piece = crop_frame(image, region)
    assert piece.shape == (20, 16, 3)
    assert np.array_equal(piece, image[10:30, 4:20])
    with pytest.raises(ValueError, match="smaller than crop region"):
        crop_frame(image, CropRegion(x=30, y=0, width=16, height=20))


@pytest.mark.parametrize("name", BOARD_FIXTURES)
def test_parse_on_crop_frame_matches_full_frame(name: str, calib: Calibration) -> None:
    """The whole point: board parsing on the crop stream == on full frames."""
    image = load_fixture(name)
    region = crop_region_for_board(calib)
    cropped = crop_calibration(calib, region)
    store = GlyphStore.load_default()
    assert parse_board(crop_frame(image, region), cropped, store) == parse_board(
        image, calib, store
    )


# -- state anchors under crop (the reason detect_state stays on full frames) --------


def test_reference_anchors_outside_board_crop(
    calib: Calibration, anchors: StateAnchors
) -> None:
    region = crop_region_for_board(calib)
    outside = anchors_outside_region(anchors, region)

    # Ground truth is GEOMETRIC, not a frozen name list: an anchor is unseen
    # on the board crop exactly when its probe patch (+/-DEFAULT_PATCH_RADIUS)
    # is not fully inside the board rectangle. Derive the expectation from the
    # measured anchor coordinates so ADDING anchors (the win/game-over popups,
    # a future AUTH screen) can't rot this test -- a new anchor above the board
    # or below it is classified automatically instead of breaking a hardcode.
    expected = tuple(
        sorted(
            name
            for name, spec in anchors.anchors.items()
            if not region.contains(spec.xy, margin=DEFAULT_PATCH_RADIUS)
        )
    )
    assert outside == expected  # also pins the sorted-tuple contract

    # The derivation must be a REAL exclusion, not a tautology: spot-check
    # hand-verified anchors that straddle the board's vertical band
    # [y=746, y=1638). Above it and below it are NOT streamed; inside it IS.
    # (Board crop = a horizontal slice; state controls live in the HUD above /
    # the button row below.) These coordinates are stable, so new anchors
    # never disturb them, yet they prove the classifier discriminates.
    assert "popup_close" in outside  # y=718, top HUD -> above the crop
    assert "new_game_btn" in outside  # y=2143, far below the crop
    assert "win_btn_left" in outside  # y=1638, exactly on the excluded bottom edge
    assert "confirm_yes" not in outside  # y=1532, inside the board band (crop SEES it)
    assert "win_card" not in outside  # y=880, inside the board band

    # Fail-closed: a crop frame can NEVER supply the full set of anchors
    # StateAnchors validation REQUIRES, because at least one required anchor
    # (popup_close/new_game_btn -- confirm_yes DOES sit inside the board) is
    # not streamed. Stated as an invariant so it survives new required anchors.
    assert set(anchors.REQUIRED) & set(outside), anchors.REQUIRED
    assert {"popup_close", "new_game_btn"} <= set(outside)


# -- env opt-in ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        (" on ", True),
        ("0", False),
        ("", False),
        ("off", False),
        ("2", False),
    ],
)
def test_crop_enabled_env_spellings(value: str, expected: bool) -> None:
    assert crop_enabled({CROP_ENV_VAR: value}) is expected


def test_crop_enabled_defaults_to_off() -> None:
    assert crop_enabled({}) is False


def test_board_crop_from_env(calib: Calibration) -> None:
    assert board_crop_from_env(calib, {}) is None  # default: nothing changes
    region = board_crop_from_env(calib, {CROP_ENV_VAR: "1"})
    assert region == crop_region_for_board(calib)

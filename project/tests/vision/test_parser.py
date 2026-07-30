"""Board parsing: fixture regressions, color fallback, error paths."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pytest
from structlog.testing import capture_logs

import bot2048.vision.parser as parser_module
from bot2048.vision.calibration import Calibration
from bot2048.vision.glyphs import GlyphStore
from bot2048.vision.parser import (
    BoardParseError,
    cell_crop,
    cell_half_size,
    learn_tile_color,
    parse_board,
    parse_board_detailed,
)
from tests.vision.conftest import Image
from tests.vision.test_states import load_incident_frame

# Ground truth for the reference-device fixture screenshots (exponents,
# row-major). Verified by eye against the images; the digit reader must
# reproduce them exactly.
EXPECTED_EXPONENTS: dict[str, tuple[int, ...]] = {
    "board_256.png": (1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 8, 4, 0, 1),
    "board_32.jpg": (0, 2, 5, 2, 0, 1, 5, 1, 0, 0, 0, 2, 0, 0, 0, 2),
    "board_a.png": (0, 1, 2, 2, 0, 3, 1, 2, 0, 1, 3, 1, 0, 1, 3, 1),
    "board_b.png": (0, 0, 2, 1, 0, 0, 3, 1, 0, 1, 3, 1, 0, 0, 3, 1),
    "board_c.png": (0, 0, 2, 1, 1, 1, 2, 1, 0, 0, 1, 2, 0, 0, 0, 3),
    "board_d_labeled.png": (0, 0, 0, 2, 1, 0, 0, 2, 0, 0, 0, 2, 0, 0, 0, 3),
    "board_fresh.png": (0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "board_full.jpg": (2, 2, 2, 1, 3, 5, 6, 2, 1, 1, 4, 3, 1, 2, 1, 2),
    "board_hi16_64.jpg": (1, 4, 6, 3, 0, 0, 3, 2, 0, 1, 0, 0, 0, 0, 0, 0),
    "live_1.png": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1),
    "live_2.png": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1),
    "live_3.png": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1),
    "state_e.png": (0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0),
}


@pytest.mark.parametrize("name", sorted(EXPECTED_EXPONENTS))
def test_fixture_boards_parse_exactly(
    name: str, load_image: Callable[[str], Image], calib: Calibration
) -> None:
    image = load_image(name)
    assert parse_board(image, calib, GlyphStore.load_default()) == EXPECTED_EXPONENTS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_EXPONENTS))
def test_fixture_reads_are_confident(
    name: str, load_image: Callable[[str], Image], calib: Calibration
) -> None:
    reads = parse_board_detailed(load_image(name), calib, GlyphStore.load_default())
    assert len(reads) == 16
    for read in reads:
        assert read.confidence >= 0.9
        if EXPECTED_EXPONENTS[name][read.index] == 0:
            assert read.method == "empty"
        else:
            assert read.method in ("digits", "color")


def test_board_full_reads_every_cell_by_digits(
    load_image: Callable[[str], Image], calib: Calibration
) -> None:
    reads = parse_board_detailed(load_image("board_full.jpg"), calib, GlyphStore.load_default())
    assert all(read.method == "digits" for read in reads)


def test_cell_read_value_property(load_image: Callable[[str], Image], calib: Calibration) -> None:
    reads = parse_board_detailed(load_image("board_fresh.png"), calib, GlyphStore.load_default())
    assert [r.value for r in reads if r.exponent] == [2, 2]
    assert all(r.value == 0 for r in reads if not r.exponent)


def test_digitless_tiles_fall_back_to_color(
    paint_board: Callable[[Sequence[int]], Image], calib: Calibration
) -> None:
    exponents = (0,) * 12 + (1, 2, 3, 0)
    reads = parse_board_detailed(paint_board(exponents), calib)
    assert tuple(r.exponent for r in reads) == exponents
    assert [r.method for r in reads[12:]] == ["color", "color", "color", "empty"]
    assert all(r.color_distance is not None for r in reads[12:15])


def test_empty_synthetic_board(
    paint_board: Callable[[Sequence[int]], Image], calib: Calibration
) -> None:
    assert parse_board(paint_board((0,) * 16), calib) == (0,) * 16


def test_unknown_tile_color_raises(
    paint_board: Callable[[Sequence[int]], Image], calib: Calibration
) -> None:
    frame = paint_board((0,) * 16).copy()
    cx, cy = calib.cell_centers[5]
    half = cell_half_size(calib)
    frame[cy - half : cy + half, cx - half : cx + half] = (0, 255, 0)
    with pytest.raises(BoardParseError, match="unknown tile"):
        parse_board(frame, calib)


def test_unknown_tile_color_lenient_reads_exponent_zero(
    paint_board: Callable[[Sequence[int]], Image], calib: Calibration
) -> None:
    """``lenient=True``: a fully unknown cell parses as exponent 0, no raise."""
    frame = paint_board((0,) * 16).copy()
    cx, cy = calib.cell_centers[5]
    half = cell_half_size(calib)
    frame[cy - half : cy + half, cx - half : cx + half] = (0, 255, 0)
    reads = parse_board_detailed(frame, calib, lenient=True)
    assert reads[5].exponent == 0
    assert reads[5].method == "unknown"
    assert all(r.method != "unknown" for i, r in enumerate(reads) if i != 5)
    assert parse_board(frame, calib, lenient=True) == (0,) * 16


def test_incident_frame_reads_both_128s_by_digits(calib: Calibration) -> None:
    """The score-1508 incident board parses exactly; 128s come from digits."""
    reads = parse_board_detailed(load_incident_frame(), calib, GlyphStore.load_default())
    # row-major: empty top row; a 2; 128 + 8; 128 + two 4s
    assert tuple(r.exponent for r in reads) == (0, 0, 0, 0, 0, 0, 0, 1, 7, 3, 0, 0, 7, 2, 2, 0)
    for read in (reads[8], reads[12]):
        assert read.digits == "128"
        assert read.method == "digits"
        # the extended palette now agrees -> no color_disagrees warning
        assert read.color_exponent == 7


def test_color_disagrees_fires_once_per_cell_value(calib: Calibration) -> None:
    """The stale-palette warning is deduped to once per (cell, digits)."""
    image = load_incident_frame()
    stripped = Calibration(
        grid_bbox=calib.grid_bbox,
        cell_centers=calib.cell_centers,
        palette=tuple(entry for entry in calib.palette if entry[1] < 7),
        screen_size=calib.screen_size,
    )
    store = GlyphStore.load_default()
    parser_module._color_disagrees_seen.clear()
    try:
        with capture_logs() as logs:
            parse_board_detailed(image, stripped, store)  # first parse: warns
            parse_board_detailed(image, stripped, store)  # repeat: debug only
        events = [e for e in logs if e["event"] == "vision.cell.color_disagrees"]
        warnings = [e for e in events if e["log_level"] == "warning"]
        assert sorted(e["cell"] for e in warnings) == [8, 12]  # once per cell
        assert all(e["log_level"] == "debug" for e in events if e not in warnings)
    finally:
        parser_module._color_disagrees_seen.clear()


def test_popup_frame_raises(load_image: Callable[[str], Image], calib: Calibration) -> None:
    with pytest.raises(BoardParseError):
        parse_board(load_image("confirm_new_game.jpg"), calib, GlyphStore.load_default())


def test_cell_crop_shape(calib: Calibration) -> None:
    half = cell_half_size(calib)
    assert half == 80
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    crop = cell_crop(frame, calib.cell_centers[0], half)
    assert crop.shape == (2 * half, 2 * half, 3)


# --- 256 regression (live run 2) + learn_tile_color self-healing hook ------


def _without_exponent(calib: Calibration, exponent: int) -> Calibration:
    """Copy of ``calib`` with all palette entries for ``exponent`` removed."""
    return Calibration(
        grid_bbox=calib.grid_bbox,
        cell_centers=calib.cell_centers,
        palette=tuple(entry for entry in calib.palette if entry[1] != exponent),
        screen_size=calib.screen_size,
    )


def test_board_256_cell12_read_by_digits(
    load_image: Callable[[str], Image], calib: Calibration
) -> None:
    """Live run 2 regression: 256 must come from the digit reader, not color."""
    reads = parse_board_detailed(load_image("board_256.png"), calib, GlyphStore.load_default())
    read = reads[12]
    assert read.digits == "256"
    assert read.exponent == 8
    assert read.method == "digits"
    assert read.confidence >= 0.9
    # the newly calibrated 256 color agrees with the digits
    assert read.color_exponent == 8


def test_board_256_emits_no_color_disagrees(
    load_image: Callable[[str], Image], calib: Calibration
) -> None:
    image = load_image("board_256.png")
    store = GlyphStore.load_default()
    parser_module._color_disagrees_seen.clear()
    try:
        with capture_logs() as logs:
            parse_board_detailed(image, calib, store)
        assert not [e for e in logs if e["event"] == "vision.cell.color_disagrees"]
    finally:
        parser_module._color_disagrees_seen.clear()


def test_learn_tile_color_extends_palette_and_persists(
    load_image: Callable[[str], Image], calib: Calibration, tmp_path: Path
) -> None:
    image = load_image("board_256.png")
    stripped = _without_exponent(calib, 8)
    path = tmp_path / "calibration.json"
    stripped.save(path)
    # a foreign key in the JSON (e.g. state_anchors) must survive the update
    data = json.loads(path.read_text(encoding="utf-8"))
    data["state_anchors"] = {"probe": [1, 2, 3]}
    path.write_text(json.dumps(data), encoding="utf-8")

    learned = learn_tile_color(stripped, image, cell=12, exponent=8, path=path)

    assert 8 in {entry[1] for entry in learned.palette}
    assert 8 not in {entry[1] for entry in stripped.palette}  # input not mutated
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["state_anchors"] == {"probe": [1, 2, 3]}
    reloaded = Calibration.load(path)
    assert {entry[1] for entry in reloaded.palette} == {entry[1] for entry in learned.palette}


def test_learn_tile_color_none_path_skips_persistence(
    load_image: Callable[[str], Image], calib: Calibration
) -> None:
    stripped = _without_exponent(calib, 8)
    learned = learn_tile_color(stripped, load_image("board_256.png"), 12, 8, path=None)
    assert 8 in {entry[1] for entry in learned.palette}


def test_learn_tile_color_known_color_is_noop(
    load_image: Callable[[str], Image], calib: Calibration, tmp_path: Path
) -> None:
    """A color already in the palette for the same exponent writes nothing."""
    path = tmp_path / "calibration.json"
    result = learn_tile_color(calib, load_image("board_256.png"), 12, 8, path=path)
    assert result is calib
    assert not path.exists()


def test_learn_tile_color_conflicting_exponent_raises(
    load_image: Callable[[str], Image], calib: Calibration
) -> None:
    """Claiming a new exponent for a color owned by another tile is rejected."""
    # cell 13 is the 16 tile; its color belongs to exponent 4, not 5
    with pytest.raises(BoardParseError):
        learn_tile_color(calib, load_image("board_256.png"), 13, 5, path=None)

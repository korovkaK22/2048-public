"""Board parsing: fixture regressions, color fallback, error paths."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from structlog.testing import capture_logs

import bot2048.vision.parser as parser_module
from bot2048.vision.calibration import Calibration
from bot2048.vision.glyphs import GlyphStore
from bot2048.vision.parser import (
    COLOR_SETTLED_MAX_DIST,
    UNKNOWN_COLOR_MAX_DIST,
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


def _blend(
    calib: Calibration, exponent: int, alpha: float, *, row: int = 1
) -> tuple[int, int, int]:
    """Palette color of ``exponent`` faded in over the row-``row`` empty bg."""
    empties = [rgb for rgb, exp in calib.palette if exp == 0]
    tile = next(rgb for rgb, exp in calib.palette if exp == exponent)
    bg = empties[row]
    return cast(
        "tuple[int, int, int]",
        tuple(round(bg[i] + alpha * (tile[i] - bg[i])) for i in range(3)),
    )


def _paint_cell(frame: Image, calib: Calibration, cell: int, rgb: tuple[int, int, int]) -> None:
    cx, cy = calib.cell_centers[cell]
    half = cell_half_size(calib) + 10
    frame[cy - half : cy + half, cx - half : cx + half] = rgb


@pytest.mark.parametrize("exponent", [1, 2, 3, 4, 5])
def test_fading_tile_never_reads_as_a_different_tile(
    exponent: int, paint_board: Callable[[Sequence[int]], Image], calib: Calibration
) -> None:
    """A tile fading in from the empty background reads as 0 or ITSELF, never
    as another tile.

    Root cause of 9 i7 games aborted VISION_MISMATCH (incidents
    20260711T172703Z, 20260711T193507Z, 20260711T202717Z, 20260712T120359Z,
    20260712T150912Z, 20260712T151359Z, 20260712T191712Z, 20260712T223052Z,
    20260713T063933Z): the spawn of the PREVIOUS move was committed as a "2"
    while the real tile was a "4". A 4 (70,71,109) fading in over the
    (21,21,36) background passes within ~12 RGB of the "2" entry (58,58,72)
    at alpha ~0.6 -- close enough for the color-only snap, too dark for the
    digit mask -- and the first frame/packet that equals prediction + a legal
    2/4 spawn is accepted. Color-only reads now require a SETTLED face
    (COLOR_SETTLED_MAX_DIST), so such a frame reads exponent 0 ("unsettled")
    and the gate simply waits for the next one.
    """
    for k in range(0, 41):
        alpha = k / 40
        frame = paint_board((0,) * 16).copy()
        _paint_cell(frame, calib, 5, _blend(calib, exponent, alpha))
        read = parse_board_detailed(frame, calib, lenient=True)[5]
        assert read.exponent in (0, exponent), (
            f"exp {exponent} at alpha {alpha:.3f} read as {read.exponent} "
            f"({read.method}, dist {read.color_distance:.1f})"
        )
        if read.exponent == exponent:
            assert read.method == "color"
            assert read.color_distance <= COLOR_SETTLED_MAX_DIST
    settled = parse_board_detailed(frame, calib, lenient=True)[5]  # alpha 1.0
    assert settled.exponent == exponent
    assert settled.method == "color"


def test_fading_4_dips_near_the_2_entry_but_stays_unsettled(
    paint_board: Callable[[Sequence[int]], Image], calib: Calibration
) -> None:
    """Pins the measured geometry behind COLOR_SETTLED_MAX_DIST: the 4-tile
    fade path's nearest entry IS the "2" for a while (alpha ~0.4..0.75), at a
    distance that stays above the settled gate but below the unknown gate."""
    frame = paint_board((0,) * 16).copy()
    _paint_cell(frame, calib, 5, _blend(calib, 2, 0.6))
    read = parse_board_detailed(frame, calib, lenient=True)[5]
    assert read.color_exponent == 1  # nearest palette color is the "2"
    assert COLOR_SETTLED_MAX_DIST < read.color_distance < UNKNOWN_COLOR_MAX_DIST
    assert read.method == "unsettled"
    assert read.exponent == 0
    assert read.confidence == 0.0
    assert read.digits is None


def test_unsettled_color_strict_raises(
    paint_board: Callable[[Sequence[int]], Image], calib: Calibration
) -> None:
    """Strict mode fails loudly on an unsettled face instead of guessing."""
    frame = paint_board((0,) * 16).copy()
    _paint_cell(frame, calib, 5, _blend(calib, 2, 0.6))
    with pytest.raises(BoardParseError, match="unsettled"):
        parse_board(frame, calib)
    # the unknown gate still owns colors far from EVERY entry
    _paint_cell(frame, calib, 5, (0, 255, 0))
    with pytest.raises(BoardParseError, match="unknown tile"):
        parse_board(frame, calib)


def test_settled_gate_has_headroom_over_real_faces(
    load_image: Callable[[str], Image], calib: Calibration
) -> None:
    """Every settled tile face on the real fixtures sits well inside the
    settled gate (measured max 3.7 on 149 i7 frames), so the gate can only
    ever reject animation/dimming, never a real settled read."""
    worst = 0.0
    for name in ("board_256.png", "board_a.png", "board_fresh.png", "game_over_before.png"):
        # lenient: game_over_before.png holds 1024 tiles the reference
        # palette does not know (read by digits, color_distance is meaningless)
        for read in parse_board_detailed(load_image(name), calib, lenient=True):
            if read.method == "color" or (read.method == "digits" and read.exponent < 9):
                worst = max(worst, read.color_distance)
    assert 0.0 < worst < COLOR_SETTLED_MAX_DIST / 2


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

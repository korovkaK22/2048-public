"""Digit-glyph store: templates, matching, serialization."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from bot2048.vision.calibration import Calibration
from bot2048.vision.glyphs import (
    MAX_DIGIT_SCORE,
    Glyph,
    GlyphStore,
    digit_components,
    normalize,
    read_number,
    white_mask,
)
from bot2048.vision.parser import cell_crop, cell_half_size
from tests.vision.conftest import Image


def test_packaged_store_contents() -> None:
    store = GlyphStore.packaged()
    assert len(store) == 19
    assert store.digits() == {"1", "2", "3", "4", "5", "6", "8"}


def test_every_packaged_template_matches_itself() -> None:
    store = GlyphStore.packaged()
    for glyph in store.glyphs:
        match = store.match(glyph.grid, glyph.aspect)
        assert match.digit == glyph.digit
        assert match.score <= 0.01  # 0 = identical; self-match is near-exact


def test_dict_round_trip() -> None:
    store = GlyphStore.packaged()
    clone = GlyphStore.from_dict(store.to_dict())
    assert clone.to_dict() == store.to_dict()
    assert clone.digits() == store.digits()


def test_file_round_trip(tmp_path: Path) -> None:
    store = GlyphStore.packaged()
    path = tmp_path / "glyphs.json"
    store.save(path)
    assert GlyphStore.load(path).to_dict() == store.to_dict()


def test_merged_extends_store() -> None:
    store = GlyphStore.packaged()
    assert len(store.merged(GlyphStore())) == len(store)
    extra = Glyph(digit="9", grid=store.glyphs[0].grid, aspect=store.glyphs[0].aspect)
    grown = store.merged(GlyphStore((extra,)))
    assert len(grown) == len(store) + 1
    assert "9" in grown.digits()


def test_load_default_without_store_file(tmp_path: Path) -> None:
    # a missing on-host store must silently fall back to packaged templates
    store = GlyphStore.load_default(tmp_path / "absent.json")
    assert store.digits() == GlyphStore.packaged().digits()


def test_white_mask_on_flat_crop_has_no_components() -> None:
    flat = np.full((60, 60, 3), (243, 112, 33), dtype=np.uint8)
    assert digit_components(white_mask(flat)) == ()


def test_synthetic_component_normalizes() -> None:
    crop = np.zeros((60, 60, 3), dtype=np.uint8)
    crop[15:45, 25:35] = 255  # a tall white bar, roughly a "1"
    comps = digit_components(white_mask(crop))
    assert len(comps) == 1
    grid, aspect = normalize(comps[0])
    assert 0.0 < aspect < 1.0  # taller than wide
    assert float(np.max(grid)) > 0.0


def test_read_number_on_real_64_tile(
    load_image: Callable[[str], Image], calib: Calibration
) -> None:
    image = load_image("board_full.jpg")
    crop = cell_crop(image, calib.cell_centers[6], cell_half_size(calib))
    read = read_number(white_mask(crop), GlyphStore.packaged())
    assert read.text == "64"
    assert read.score <= MAX_DIGIT_SCORE  # distance convention: lower is better
    assert len(read.matches) == 2


@pytest.mark.parametrize("index,text", [(0, "4"), (3, "2"), (5, "32"), (10, "16")])
def test_read_number_across_board_full(
    index: int, text: str, load_image: Callable[[str], Image], calib: Calibration
) -> None:
    image = load_image("board_full.jpg")
    crop = cell_crop(image, calib.cell_centers[index], cell_half_size(calib))
    assert read_number(white_mask(crop), GlyphStore.packaged()).text == text

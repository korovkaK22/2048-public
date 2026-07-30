"""Calibration dataclass: invariants, serialization, load errors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot2048.vision.calibration import Calibration, CalibrationError


def test_reference_geometry_invariants(calib: Calibration) -> None:
    left, top, right, bottom = calib.grid_bbox
    assert calib.screen_size == (1080, 2400)
    assert len(calib.cell_centers) == 16
    # the grid is a square panel, centers laid out on a 4x4 lattice inside it
    assert abs((right - left) - (bottom - top)) <= 2
    xs = sorted({x for x, _ in calib.cell_centers})
    ys = sorted({y for _, y in calib.cell_centers})
    assert len(xs) == 4 and len(ys) == 4
    assert all(left < x < right for x in xs)
    assert all(top < y < bottom for y in ys)


def test_palette_covers_expected_exponents(calib: Calibration) -> None:
    exponents = {exp for _, exp in calib.palette}
    assert {0, 1, 2, 3, 4, 5, 6} <= exponents
    for rgb, _ in calib.palette:
        assert all(0 <= c <= 255 for c in rgb)


def test_dict_round_trip(calib: Calibration) -> None:
    clone = Calibration.from_dict(calib.to_dict())
    assert clone.to_dict() == calib.to_dict()
    assert clone.cell_centers == calib.cell_centers
    assert clone.palette == calib.palette


def test_file_round_trip(tmp_path: Path, calib: Calibration) -> None:
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(calib.to_dict()), encoding="utf-8")
    assert Calibration.load(path).to_dict() == calib.to_dict()


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError):
        Calibration.load(tmp_path / "nope.json")


def test_from_dict_malformed_raises(calib: Calibration) -> None:
    data = calib.to_dict()
    del data["cell_centers"]
    with pytest.raises(CalibrationError):
        Calibration.from_dict(data)

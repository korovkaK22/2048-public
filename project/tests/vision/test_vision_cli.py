"""``bot2048 vision`` CLI: calibrate / state / parse / add-glyphs."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bot2048.vision.cli import app
from bot2048.vision.glyphs import GlyphStore
from tests.vision.conftest import FIXTURES

needs_backend = pytest.mark.skipif(
    importlib.util.find_spec("cv2") is None and importlib.util.find_spec("PIL") is None,
    reason="image commands need cv2 or Pillow",
)

runner = CliRunner()


def _calibrate(tmp_path: Path) -> Path:
    out = tmp_path / "calibration.json"
    result = runner.invoke(app, ["calibrate", "--out", str(out)])
    assert result.exit_code == 0, result.output
    return out


def test_calibrate_writes_reference_bundle(tmp_path: Path) -> None:
    out = _calibrate(tmp_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["cell_centers"]) == 16
    assert set(data["state_anchors"]["anchors"]) >= {
        "confirm_yes",
        "popup_close",
        "new_game_btn",
    }


@needs_backend
def test_calibrate_detects_geometry_from_image(tmp_path: Path) -> None:
    out = tmp_path / "calibration.json"
    result = runner.invoke(
        app,
        ["calibrate", "--image", str(FIXTURES / "board_fresh.png"), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "detected panel" in result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["grid_bbox"] == [107, 760, 973, 1625]


@needs_backend
def test_calibrate_rejects_popup_image(tmp_path: Path) -> None:
    out = tmp_path / "calibration.json"
    result = runner.invoke(
        app,
        ["calibrate", "--image", str(FIXTURES / "confirm_new_game.jpg"), "--out", str(out)],
    )
    assert result.exit_code == 1
    assert not out.exists()


@needs_backend
def test_state_command(tmp_path: Path) -> None:
    calibration = _calibrate(tmp_path)
    result = runner.invoke(
        app,
        ["state", str(FIXTURES / "confirm_new_game.jpg"), "-c", str(calibration)],
    )
    assert result.exit_code == 0, result.output
    assert "confirm_new_game" in result.output


@needs_backend
def test_parse_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Rich console width AND colour detection are inherited from the
    # terminal/env and differ between xdist workers: a wrapped line or ANSI
    # escapes injected mid-tuple would break the substring check.  Pin width,
    # disable colour (rich honours NO_COLOR), strip any residual escapes and
    # compare whitespace-normalised output (xdist-safe, serial-identical).
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    calibration = _calibrate(tmp_path)
    result = runner.invoke(
        app,
        ["parse", str(FIXTURES / "board_full.jpg"), "-c", str(calibration)],
    )
    assert result.exit_code == 0, result.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    flat = " ".join(plain.split())
    assert "64" in flat
    assert "(2, 2, 2, 1, 3, 5, 6, 2, 1, 1, 4, 3, 1, 2, 1, 2)" in flat


def test_parse_missing_image_fails(tmp_path: Path) -> None:
    calibration = _calibrate(tmp_path)
    result = runner.invoke(app, ["parse", str(tmp_path / "nope.png"), "-c", str(calibration)])
    assert result.exit_code == 1


@needs_backend
def test_add_glyphs_extends_store(tmp_path: Path) -> None:
    calibration = _calibrate(tmp_path)
    store_path = tmp_path / "glyphs.json"
    result = runner.invoke(
        app,
        [
            "add-glyphs",
            str(FIXTURES / "board_full.jpg"),
            "--cell",
            "r1c2=64",
            "-c",
            str(calibration),
            "--store",
            str(store_path),
        ],
    )
    assert result.exit_code == 0, result.output
    store = GlyphStore.load(store_path)
    assert store.digits() == {"4", "6"}
    assert len(store) == 2


@needs_backend
def test_add_glyphs_rejects_component_mismatch(tmp_path: Path) -> None:
    calibration = _calibrate(tmp_path)
    result = runner.invoke(
        app,
        [
            "add-glyphs",
            str(FIXTURES / "board_full.jpg"),
            "--cell",
            "r0c0=444",  # cell r0c0 shows "4": one component, not three
            "-c",
            str(calibration),
            "--store",
            str(tmp_path / "glyphs.json"),
        ],
    )
    assert result.exit_code == 1


@pytest.mark.parametrize("bad", ["r0c0=1", "r4c0=4", "x0c0=4", "r0c0", "r0c0=abc"])
def test_add_glyphs_rejects_bad_labels(tmp_path: Path, bad: str) -> None:
    result = runner.invoke(
        app,
        ["add-glyphs", str(FIXTURES / "board_full.jpg"), "--cell", bad],
    )
    assert result.exit_code == 2

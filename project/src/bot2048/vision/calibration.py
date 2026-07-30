"""Screen calibration: where the 4x4 board is and what each tile color means.

A :class:`Calibration` is produced once per device/resolution from real
screenshots (see ``screens/`` and ``docs/live-setup.md``) and persisted as
JSON (default: ``data/calibration.json``). It contains:

* the board bounding box in screen pixels,
* the 16 cell centers (row-major, matching :data:`bot2048.core.types.Board`),
* a color palette mapping a representative RGB color to a tile exponent
  (``0`` = empty cell background).

Known monobank clone facts (package ``com.ftband.mono``, 1080x2400 screen):
tiles have clearly distinct colors -- see :data:`KNOWN_TILE_COLOR_NAMES`.
Exact RGB values MUST be measured from screenshots, never hardcoded here.

Everything in this module works without a phone: it is pure file I/O and
validation, fully covered by the type checker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot2048.core.types import BOARD_CELLS

#: Default on-host location of the calibration file (gitignored ``data/``).
DEFAULT_CALIBRATION_PATH: Path = Path("data") / "calibration.json"

#: Screen size of the reference device used for the first calibration.
DEFAULT_SCREEN_SIZE: tuple[int, int] = (1080, 2400)

#: Human observations from ``screens/`` -- a checklist for calibration, not data.
#: (exponent -> approximate color name; exact RGB comes from measurement)
KNOWN_TILE_COLOR_NAMES: dict[int, str] = {
    1: "dark gray (tile 2)",
    2: "slate gray-violet (tile 4)",
    3: "blue (tile 8)",
    4: "teal (tile 16)",
    5: "green (tile 32)",
    6: "orange (tile 64)",
    7: "vivid orange (tile 128)",  # measured 2026-07-10; close to tile 64
    8: "deep orange (tile 256)",  # measured from board_256.png (live run 2)
}

Rgb = tuple[int, int, int]
#: ``((r, g, b), exponent)``; exponent ``0`` marks the empty-cell background.
PaletteEntry = tuple[Rgb, int]


class CalibrationError(ValueError):
    """Calibration data is missing, malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class Calibration:
    """Pixel-space description of the board on one specific device.

    Attributes:
        grid_bbox: ``(left, top, right, bottom)`` of the board in pixels,
            right/bottom exclusive. Used for swipe geometry (Humanizer).
        cell_centers: 16 ``(x, y)`` pixel centers, row-major
            (``index = row * 4 + col``), matching the ``Board`` layout.
        palette: representative colors -> tile exponent. May contain several
            entries per exponent (e.g. anti-aliased shades). Must include an
            entry with exponent ``0`` for the empty cell background.
        screen_size: ``(width, height)`` the calibration was made for; a
            safety check against screenshots from a different device/DPI.
    """

    grid_bbox: tuple[int, int, int, int]
    cell_centers: tuple[tuple[int, int], ...]
    palette: tuple[PaletteEntry, ...]
    screen_size: tuple[int, int] = DEFAULT_SCREEN_SIZE

    def __post_init__(self) -> None:
        left, top, right, bottom = self.grid_bbox
        if not (left < right and top < bottom):
            raise CalibrationError(f"degenerate grid_bbox {self.grid_bbox!r}")
        if len(self.cell_centers) != BOARD_CELLS:
            raise CalibrationError(
                f"expected {BOARD_CELLS} cell centers, got {len(self.cell_centers)}"
            )
        if not self.palette:
            raise CalibrationError("palette must not be empty")
        for rgb, exponent in self.palette:
            if exponent < 0:
                raise CalibrationError(f"negative exponent {exponent} in palette")
            if len(rgb) != 3 or any(not (0 <= c <= 255) for c in rgb):
                raise CalibrationError(f"invalid RGB {rgb!r} in palette")
        if all(exp != 0 for _, exp in self.palette):
            raise CalibrationError("palette must contain an empty-cell entry (exponent 0)")
        w, h = self.screen_size
        if w <= 0 or h <= 0:
            raise CalibrationError(f"invalid screen_size {self.screen_size!r}")

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON representation (lists instead of tuples)."""
        return {
            "grid_bbox": list(self.grid_bbox),
            "cell_centers": [list(c) for c in self.cell_centers],
            "palette": [{"rgb": list(rgb), "exponent": exp} for rgb, exp in self.palette],
            "screen_size": list(self.screen_size),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Calibration:
        """Inverse of :meth:`to_dict`. Raises :class:`CalibrationError` on bad input."""
        try:
            left, top, right, bottom = (int(v) for v in data["grid_bbox"])
            centers = tuple((int(x), int(y)) for x, y in data["cell_centers"])
            palette = tuple(
                ((int(e["rgb"][0]), int(e["rgb"][1]), int(e["rgb"][2])), int(e["exponent"]))
                for e in data["palette"]
            )
            sw, sh = (int(v) for v in data.get("screen_size", DEFAULT_SCREEN_SIZE))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise CalibrationError(f"malformed calibration data: {exc}") from exc
        return cls(
            grid_bbox=(left, top, right, bottom),
            cell_centers=centers,
            palette=palette,
            screen_size=(sw, sh),
        )

    def save(self, path: Path = DEFAULT_CALIBRATION_PATH) -> None:
        """Write the calibration as pretty-printed JSON (creates parent dirs)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path = DEFAULT_CALIBRATION_PATH) -> Calibration:
        """Load a calibration file. Raises :class:`CalibrationError` if absent/bad."""
        if not path.is_file():
            raise CalibrationError(
                f"calibration file not found: {path} -- run `bot2048 vision calibrate` "
                "(see docs/live-setup.md)"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CalibrationError(f"calibration file {path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise CalibrationError(f"calibration file {path} must contain a JSON object")
        return cls.from_dict(data)

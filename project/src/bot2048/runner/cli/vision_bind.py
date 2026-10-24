"""Calibration-bound vision for the live CLI.

The concrete vision implementation (``detect_state`` / ``parse_board``) is
bound to the calibration HERE and injected into the runner behind its local
:class:`~bot2048.runner.live.StateDetector` / ``BoardReader`` protocols -- the
loop itself never imports the parser, so vision/ can keep moving.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import typer

from bot2048.core.types import Board
from bot2048.runner.cli.common import err_console, log
from bot2048.runner.incidents import Image
from bot2048.runner.live import CellLook
from bot2048.vision.calibration import CalibrationError
from bot2048.vision.parser import (
    BoardParseError,
    cell_crop,
    cell_half_size,
    learn_tile_color,
    parse_board,
    parse_board_detailed,
)
from bot2048.vision.states import StateDetection, detect_state, load_calibration_bundle

LEARNED_TILES_DIR: Path = Path("logs") / "learned_tiles"
"""Best-effort PNG crops of every auto-learned tile (human audit trail)."""


class _Vision:
    """Calibration-bound vision functions with a SWAPPABLE calibration.

    ``learn_tile`` (the runner's auto-learn hook) replaces
    :attr:`calibration` in place after ``vision.learn_tile_color`` accepts a
    new tile color, so ``detect``/``read``/``read_cells`` immediately parse
    with the extended palette -- and the JSON at ``path`` is persisted by
    vision itself, surviving restarts.
    """

    def __init__(self, path: Path) -> None:
        try:
            self.calibration, self._anchors = load_calibration_bundle(path)
        except CalibrationError as exc:
            err_console.print(f"[red]calibration error:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        self.path = path

    def detect(self, image: Image) -> StateDetection:
        return detect_state(image, self.calibration, self._anchors)

    def read(self, image: Image) -> Board:
        # Lenient: a fully unknown tile reads as exponent 0 instead of
        # raising, so live.py reaches its mismatch branch and the
        # auto-learn gate (_try_learn_new_tiles) instead of aborting the
        # whole game with VISION_MISMATCH.
        return parse_board(image, self.calibration, lenient=True)

    def read_cells(self, image: Image) -> tuple[CellLook, ...] | None:
        """Per-cell trust info for auto-learn (``None`` = no detail available)."""
        try:
            reads = parse_board_detailed(image, self.calibration, lenient=True)
        except (BoardParseError, ValueError):
            return None
        return tuple(
            CellLook(exponent=r.exponent, suspect=r.method != "digits") for r in reads
        )

    def learn_tile(self, frame: Image, cell: int, exponent: int) -> bool:
        """Teach one tile color; ``False`` = vision refused the sample."""
        try:
            self.calibration = learn_tile_color(
                self.calibration, frame, cell, exponent, path=self.path
            )
        except BoardParseError:
            return False
        self._save_learned_crop(frame, cell, exponent)
        return True

    def _save_learned_crop(self, frame: Image, cell: int, exponent: int) -> None:
        """Best-effort PNG of the just-learned cell crop.

        Saved under ``logs/learned_tiles/`` so a human can audit what color
        sample entered the palette. Mirrors the incident-bundle rule: an
        artifact failure is logged and skipped -- it must NEVER undo or mask
        an accepted learn (the calibration is already persisted).
        """
        try:
            crop = cell_crop(
                frame,
                self.calibration.cell_centers[cell],
                cell_half_size(self.calibration),
            )
            LEARNED_TILES_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = LEARNED_TILES_DIR / f"{stamp}_cell{cell:02d}_value{2**exponent}.png"
            pil_image = importlib.import_module("PIL.Image")
            pil_image.fromarray(crop).save(path)
            log.info(
                "live.tile_crop_saved", file=str(path), cell=cell, exponent=exponent
            )
        except Exception as exc:  # Pillow missing / crop error -- never fatal
            log.warning("live.tile_crop_failed", cell=cell, error=str(exc))


def _make_vision(path: Path) -> _Vision:
    """Load calibration + state anchors and bind the concrete vision functions."""
    return _Vision(path)

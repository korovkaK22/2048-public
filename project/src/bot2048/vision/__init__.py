"""Vision layer: screenshot -> Board via digit glyphs + calibrated colors.

Public seam used by the runner (bot2048.runner):

    calib, anchors = load_calibration_bundle(path)      # data/calibration.json
    board = parse_board(image, calib)                    # -> Board (exponents)
    detection = detect_state(image, calib, anchors)      # -> StateDetection
    detection.state                                      # -> ScreenState

NOTE for integrators: ``detect_state`` returns a :class:`StateDetection`
record (``.state`` is the :class:`ScreenState`, plus ``.reason`` and
``.board_match_frac`` for logging) -- not a bare ScreenState.

Modules:
    calibration     -- Calibration dataclass + JSON persistence
    grid            -- detect_grid: find the board panel in a raw frame
    glyphs          -- digit-template matching (generalizes to unseen tiles)
    glyph_templates -- generated built-in templates (do not hand-edit)
    parser          -- parse_board: digits first, palette color fallback
    states          -- ScreenState detection (anchor colors + board match)
    cli             -- ``bot2048 vision`` typer sub-app
"""

from bot2048.vision.calibration import Calibration, CalibrationError
from bot2048.vision.glyphs import GlyphError, GlyphStore
from bot2048.vision.grid import GridGeometry, detect_grid
from bot2048.vision.parser import (
    BoardParseError,
    CellRead,
    learn_tile_color,
    parse_board,
    parse_board_detailed,
)
from bot2048.vision.states import (
    ScreenState,
    StateAnchors,
    StateDetection,
    detect_state,
    load_calibration_bundle,
)

__all__ = [
    "BoardParseError",
    "Calibration",
    "CalibrationError",
    "CellRead",
    "GlyphError",
    "GlyphStore",
    "GridGeometry",
    "ScreenState",
    "StateAnchors",
    "StateDetection",
    "detect_grid",
    "detect_state",
    "learn_tile_color",
    "load_calibration_bundle",
    "parse_board",
    "parse_board_detailed",
]

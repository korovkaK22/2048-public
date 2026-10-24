"""Board recognition: screenshot -> :data:`bot2048.core.types.Board`.

Two independent signals per cell, digits first, color as fallback:

1. **Digits** (:mod:`bot2048.vision.glyphs`): the white digit glyphs printed
   on every tile are template-matched and composed into the tile value.
   This generalizes to tiles the bot has NEVER seen (128, 256, ...) whose
   face color is not in the calibrated palette yet -- only the digit shapes
   need to be known.
2. **Color**: the median RGB of the NON-white pixels of the cell crop is
   classified against the calibrated palette (nearest neighbor). The median
   ignores both the digit glyphs and the cat mascot's white paw hanging
   over the top board row, so it is measured at the TRUE cell centers.

Decision per cell (see :func:`read_cell`):

* digits readable and a power of two           -> tile (method ``digits``);
  a disagreeing palette color is logged, digits win (palette may be stale).
* no digit components + background near palette-empty -> empty cell.
* digits unreadable (occluded by the cat / partially animated) but the
  background matches a non-empty palette color -> tile (method ``color``,
  logged as a fallback).
* digits unreadable and the background is NEAR a palette entry but not ON
  it (farther than :data:`COLOR_SETTLED_MAX_DIST`) -> the cell is
  UNSETTLED: a tile mid fade-in / mid-merge, or a dimmed screen. Strict
  mode raises; lenient mode yields ``method="unsettled"``, exponent 0, so
  the live gate skips the frame and re-captures instead of committing a
  wrong value. A 4-tile fading in from the empty background passes within
  ~12 RGB of the palette "2" (alpha ~0.6) -- read as a settled 2 it
  silently corrupts the tracked board (i7 run, 9 games aborted
  VISION_MISMATCH one move later; docs/device-ui-notes.md).
* digits unreadable AND the background is far from every palette entry
  -> :class:`BoardParseError`. The parser fails LOUDLY instead of guessing:
  the live loop re-screenshots on parse errors, and a persistent error
  means the palette/glyph store must be extended (``bot2048 vision
  add-glyphs`` / recalibrate) -- exactly the "unknown tile" situation.

This module needs numpy only (structlog for warnings); no phone, no cv2 --
fully testable with synthetic arrays. Images are RGB uint8 ``(H, W, 3)``;
the CLI converts from cv2's BGR on load.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

import numpy as np
import numpy.typing as npt
import structlog

from bot2048.core.types import BOARD_CELLS, Board
from bot2048.vision.calibration import DEFAULT_CALIBRATION_PATH, Calibration, PaletteEntry
from bot2048.vision.glyphs import GlyphStore, NumberRead, read_number, white_mask

__all__ = [
    "COLOR_SETTLED_MAX_DIST",
    "DEFAULT_PATCH_RADIUS",
    "LEARN_MIN_SEPARATION",
    "UNKNOWN_COLOR_MAX_DIST",
    "BoardParseError",
    "CellRead",
    "Image",
    "cell_crop",
    "cell_half_size",
    "learn_tile_color",
    "mean_patch_color",
    "nearest_exponent",
    "nearest_palette_entry",
    "parse_board",
    "parse_board_detailed",
    "read_cell",
    "tile_background_color",
]

_log = structlog.get_logger(__name__)

#: Screenshot type used across vision: RGB, uint8, shape (H, W, 3).
Image = npt.NDArray[np.uint8]

#: Half-size of the square patch sampled around a probe point (state
#: anchors and legacy color probes): 6 -> a 13x13 patch.
DEFAULT_PATCH_RADIUS: int = 6

#: Cell crop half-size as a fraction of the center-to-center pitch
#: (0.37 * 216.3 px = 80 px on the reference device -- covers the digits
#: with margin while staying inside the tile face).
CROP_HALF_FRACTION: float = 0.37

#: A cell whose background is farther (RGB Euclidean) than this from every
#: palette entry has an UNKNOWN tile color; without readable digits the
#: parser raises instead of guessing. Measured: real backgrounds sit within
#: ~3 of their palette entry, the closest WRONG entry is > 25 away.
UNKNOWN_COLOR_MAX_DIST: float = 60.0

#: A color-only read (digits unreadable) is trusted only when the background
#: sits within this RGB distance of its palette entry -- a SETTLED tile face.
#: Measured on 149 real i7 frames (~1 700 digit-confirmed cells): settled
#: faces are <= 3.7 from their entry. Beyond this and below
#: :data:`UNKNOWN_COLOR_MAX_DIST` the cell is mid-animation or dimmed: a tile
#: fading in from the empty background sweeps through the palette line, and
#: the 4-tile path comes within 11.7..12.9 of the "2" entry (alpha ~0.6) --
#: the color-only snap then reads a settled "2" for a tile that is really a
#: 4. 8.0 leaves ~2x headroom over settled faces and ~1.5x below that dip.
COLOR_SETTLED_MAX_DIST: float = 8.0

#: :func:`learn_tile_color` refuses a sample closer (RGB Euclidean) than
#: this to a palette entry of a DIFFERENT exponent: settled tile faces sit
#: within ~3 of their own entry and the closest WRONG entry is > 25 away
#: (the tightest real pair, 128 vs 256 orange, is ~41 apart), so a sample
#: this close to another tile's color means an unsettled frame or a
#: mislabeled cell. A sample this close to a SAME-exponent entry is a
#: duplicate and the calibration is returned unchanged.
LEARN_MIN_SEPARATION: float = 12.0

#: Method that decided a cell (see module docstring). ``"unknown"`` and
#: ``"unsettled"`` only appear in lenient mode (strict mode raises instead):
#: ``"unknown"`` -- digits unreadable AND the background color is farther than
#: :data:`UNKNOWN_COLOR_MAX_DIST` from every palette entry; ``"unsettled"`` --
#: digits unreadable and the background is between :data:`COLOR_SETTLED_MAX_DIST`
#: and that gate (mid-animation / dimmed frame). Both read as exponent 0 with
#: confidence 0.0; consumers treat every non-``"digits"`` method as suspect.
CellMethod = Literal["digits", "color", "empty", "unknown", "unsettled"]

#: ``(cell index, digit text)`` pairs the ``vision.cell.color_disagrees``
#: warning already fired for. The first occurrence per pair logs at WARNING,
#: repeats drop to DEBUG -- on the first live run the un-throttled warning
#: fired hundreds of times for the same two 128 cells.
_color_disagrees_seen: set[tuple[int, str]] = set()


class BoardParseError(ValueError):
    """The image cannot be interpreted as a board with this calibration."""


def _validate_image(image: Image) -> None:
    if image.ndim != 3 or image.shape[2] < 3:
        raise BoardParseError(f"expected an (H, W, 3) RGB image, got shape {image.shape!r}")


def mean_patch_color(
    image: Image,
    center: tuple[int, int],
    radius: int = DEFAULT_PATCH_RADIUS,
) -> tuple[float, float, float]:
    """Mean RGB color of the square patch of half-size ``radius`` around ``center``.

    ``center`` is ``(x, y)`` in pixels. The patch is clipped to the image;
    a center outside the image raises :class:`BoardParseError`.
    """
    _validate_image(image)
    if radius < 0:
        raise ValueError(f"radius must be >= 0, got {radius}")
    x, y = center
    h, w = image.shape[0], image.shape[1]
    x0, x1 = max(x - radius, 0), min(x + radius + 1, w)
    y0, y1 = max(y - radius, 0), min(y + radius + 1, h)
    if x0 >= x1 or y0 >= y1:
        raise BoardParseError(f"cell center {center!r} lies outside the {w}x{h} image")
    patch = image[y0:y1, x0:x1, :3].astype(np.float64)
    mean = patch.reshape(-1, 3).mean(axis=0)
    return (float(mean[0]), float(mean[1]), float(mean[2]))


def nearest_palette_entry(
    color: tuple[float, float, float],
    palette: tuple[PaletteEntry, ...],
) -> PaletteEntry:
    """Palette entry with the smallest squared RGB distance to ``color``."""
    if not palette:
        raise BoardParseError("empty palette")

    def sq_dist(entry: PaletteEntry) -> float:
        (r, g, b), _ = entry
        return (r - color[0]) ** 2 + (g - color[1]) ** 2 + (b - color[2]) ** 2

    return min(palette, key=sq_dist)


def nearest_exponent(
    color: tuple[float, float, float],
    palette: tuple[PaletteEntry, ...],
) -> int:
    """Tile exponent of the nearest palette color (``0`` = empty cell)."""
    return nearest_palette_entry(color, palette)[1]


def cell_half_size(calibration: Calibration) -> int:
    """Cell-crop half-size in px, derived from the calibrated center pitch."""
    xs = sorted({x for x, _ in calibration.cell_centers})
    if len(xs) < 2:
        raise BoardParseError("calibration cell centers are degenerate (no column pitch)")
    pitch = (xs[-1] - xs[0]) / (len(xs) - 1)
    half = round(CROP_HALF_FRACTION * pitch)
    if half < 4:
        raise BoardParseError(f"implausible cell pitch {pitch:.1f}px in calibration")
    return half


def cell_crop(image: Image, center: tuple[int, int], half: int) -> Image:
    """Square crop of half-size ``half`` around ``center``, clipped to the image."""
    _validate_image(image)
    x, y = center
    h, w = image.shape[0], image.shape[1]
    x0, x1 = max(x - half, 0), min(x + half, w)
    y0, y1 = max(y - half, 0), min(y + half, h)
    if x1 - x0 < half or y1 - y0 < half:
        raise BoardParseError(f"cell at {center!r} lies (mostly) outside the {w}x{h} image")
    return image[y0:y1, x0:x1]


#: Fraction of crop rows dropped from the TOP before the background median.
#: The cat's paw rests on the top edge of the board and occludes top-row
#: cells from above, so the lower rows are the trustworthy ones.
PAW_SKIP_TOP_FRACTION: float = 0.40


def tile_background_color(crop: Image) -> tuple[float, float, float]:
    """Median RGB of the non-white pixels: the tile face / cell background.

    Only the lower ``1 - PAW_SKIP_TOP_FRACTION`` of the crop rows is used:
    the cat's paw drapes over the top edge of top-row cells, and its white
    fur can dominate the upper half of the crop. The median over the
    remaining rows is immune to the white digit glyphs AND to residual
    partial white occlusions, unlike a mean patch.
    """
    skip = int(crop.shape[0] * PAW_SKIP_TOP_FRACTION)
    lower = crop[skip:, :, :3]
    if lower.shape[0] < 2:  # degenerate tiny crop: use everything
        lower = crop[:, :, :3]
    flat = lower.reshape(-1, 3)
    non_white = flat[flat.min(axis=1) < 170]
    if non_white.shape[0] < 0.05 * flat.shape[0]:  # nearly all white: popup card etc.
        non_white = flat
    med = np.median(non_white.astype(np.float64), axis=0)
    return (float(med[0]), float(med[1]), float(med[2]))


@dataclass(frozen=True, slots=True)
class CellRead:
    """Everything the parser learned about one cell (for logs/debugging)."""

    index: int  #: 0..15 row-major cell index
    exponent: int  #: final decision (0 = empty)
    method: CellMethod
    confidence: float  #: 1.0 = perfect template match / exact palette hit
    digits: str | None  #: accepted digit reading, if any
    color_exponent: int  #: what color alone said
    color_distance: float  #: RGB distance from the background to its palette entry

    @property
    def value(self) -> int:
        """Human-readable tile value (0 for an empty cell)."""
        return 0 if self.exponent == 0 else 2**self.exponent


def read_cell(
    image: Image,
    index: int,
    center: tuple[int, int],
    half: int,
    palette: tuple[PaletteEntry, ...],
    glyphs: GlyphStore,
    *,
    lenient: bool = False,
) -> CellRead:
    """Classify a single cell (see the module docstring for the decision tree).

    With ``lenient=True`` a fully unknown cell (digits unreadable AND the
    background farther than :data:`UNKNOWN_COLOR_MAX_DIST` from every
    palette entry) does not raise; it yields ``method="unknown"``,
    ``exponent=0``, ``confidence=0.0`` so callers (the live runner) can
    keep parsing and auto-learn the tile instead of aborting. Likewise an
    UNSETTLED cell (digits unreadable, background farther than
    :data:`COLOR_SETTLED_MAX_DIST` from its nearest entry) yields
    ``method="unsettled"``, exponent 0 -- never the nearest color's value.
    """
    crop = cell_crop(image, center, half)
    background = tile_background_color(crop)
    (pr, pg, pb), color_exp = nearest_palette_entry(background, palette)
    color_dist = math.dist(background, (pr, pg, pb))

    number: NumberRead = read_number(white_mask(crop), glyphs)
    if number.text is not None:
        value = int(number.text)
        if value >= 2 and value & (value - 1) == 0:
            exponent = value.bit_length() - 1
            if color_exp != exponent:
                key = (index, number.text)
                log = _log.debug if key in _color_disagrees_seen else _log.warning
                _color_disagrees_seen.add(key)
                log(
                    "vision.cell.color_disagrees",
                    cell=index,
                    digits=number.text,
                    color_exponent=color_exp,
                    color_distance=round(color_dist, 1),
                    background=tuple(round(c) for c in background),
                    hint="palette may be missing this tile -- recalibrate",
                )
            return CellRead(
                index=index,
                exponent=exponent,
                method="digits",
                confidence=1.0 - number.score,
                digits=number.text,
                color_exponent=color_exp,
                color_distance=color_dist,
            )
        _log.warning(
            "vision.cell.not_power_of_two", cell=index, digits=number.text, score=number.score
        )

    if color_dist > UNKNOWN_COLOR_MAX_DIST:
        if lenient:
            _log.warning(
                "vision.cell.unknown_tile",
                cell=index,
                background=tuple(round(c) for c in background),
                color_distance=round(color_dist, 1),
                digit_matches=number.matches,
                decided="unknown (lenient mode, exponent 0)",
            )
            return CellRead(
                index=index,
                exponent=0,
                method="unknown",
                confidence=0.0,
                digits=None,
                color_exponent=color_exp,
                color_distance=color_dist,
            )
        raise BoardParseError(
            f"cell {index}: digits unreadable (matches={number.matches!r}) and background "
            f"rgb{tuple(round(c) for c in background)} is {color_dist:.0f} away from every "
            "palette entry -- unknown tile; extend the glyph store / palette "
            "(`bot2048 vision add-glyphs` / `bot2048 vision calibrate`)"
        )
    if color_dist > COLOR_SETTLED_MAX_DIST:
        # Near a palette entry but not on it: a tile mid fade-in / mid-merge
        # or a dimmed frame. Snapping to the nearest color here is exactly
        # how a fading 4 was committed as a 2 (see COLOR_SETTLED_MAX_DIST).
        if lenient:
            # DEBUG: the live predict-gate polls every ~50 ms through an
            # animation, so this fires on normal frames; the gate simply
            # skips them and re-captures.
            _log.debug(
                "vision.cell.unsettled_color",
                cell=index,
                nearest_exponent=color_exp,
                color_distance=round(color_dist, 1),
                background=tuple(round(c) for c in background),
                decided="unsettled (lenient mode, exponent 0)",
            )
            return CellRead(
                index=index,
                exponent=0,
                method="unsettled",
                confidence=0.0,
                digits=None,
                color_exponent=color_exp,
                color_distance=color_dist,
            )
        raise BoardParseError(
            f"cell {index}: digits unreadable (matches={number.matches!r}) and background "
            f"rgb{tuple(round(c) for c in background)} is {color_dist:.1f} away from its "
            f"nearest palette entry (exponent {color_exp}) -- unsettled frame "
            "(mid-animation or dimmed), not a settled tile face"
        )
    confidence = 1.0 - color_dist / UNKNOWN_COLOR_MAX_DIST
    if color_exp == 0:
        if number.matches:
            _log.warning(
                "vision.cell.glyphs_on_empty", cell=index, score=number.score, decided="empty"
            )
        return CellRead(
            index=index,
            exponent=0,
            method="empty",
            confidence=confidence,
            digits=None,
            color_exponent=0,
            color_distance=color_dist,
        )
    # DEBUG (not INFO): a color fallback can fire on several cells EVERY move
    # (animation/occlusion), so at the default INFO run level this high-frequency
    # per-move line is gated off the hot path; --verbose brings it back. The
    # per-move vision-fallback picture is still recorded in the DB
    # (eval_details["vision"]: suspect count/mask/exps), so telemetry is intact.
    _log.debug(
        "vision.cell.color_fallback",
        cell=index,
        exponent=color_exp,
        color_distance=round(color_dist, 1),
        digit_score=round(number.score, 3),
        reason="digits unreadable (occluded or animating)",
    )
    return CellRead(
        index=index,
        exponent=color_exp,
        method="color",
        confidence=confidence,
        digits=None,
        color_exponent=color_exp,
        color_distance=color_dist,
    )


def parse_board_detailed(
    image: Image,
    calibration: Calibration,
    glyphs: GlyphStore | None = None,
    *,
    lenient: bool = False,
) -> tuple[CellRead, ...]:
    """Per-cell detail for all 16 cells (row-major, matching ``Board``).

    The caller is responsible for making sure the image actually shows the
    board (see :mod:`bot2048.vision.states`); popups raise or produce
    garbage otherwise.

    With ``lenient=True`` a fully unknown cell yields
    ``method="unknown"``/``exponent=0`` and an unsettled cell
    ``method="unsettled"``/``exponent=0`` instead of raising (see
    :func:`read_cell`) -- the live runner uses this so a freshly merged
    tile the palette does not know yet reaches the auto-learn gate, and a
    mid-animation frame is skipped rather than committed. The strict
    default stays the calibration/test contract.

    Raises:
        BoardParseError: wrong image size for this calibration, or (strict
            mode only) a cell with unreadable digits AND an unknown or
            unsettled background color.
    """
    _validate_image(image)
    h, w = image.shape[0], image.shape[1]
    cw, ch = calibration.screen_size
    if (w, h) != (cw, ch):
        raise BoardParseError(
            f"image is {w}x{h} but calibration was made for {cw}x{ch}; recalibrate"
        )
    store = glyphs if glyphs is not None else GlyphStore.load_default()
    half = cell_half_size(calibration)
    reads = tuple(
        read_cell(image, i, center, half, calibration.palette, store, lenient=lenient)
        for i, center in enumerate(calibration.cell_centers)
    )
    assert len(reads) == BOARD_CELLS
    return reads


def learn_tile_color(
    calibration: Calibration,
    image: Image,
    cell: int,
    exponent: int,
    path: Path | None = DEFAULT_CALIBRATION_PATH,
) -> Calibration:
    """Extend the palette with the measured face color of one known tile.

    Self-healing hook for the runner: when the engine KNOWS a cell's value
    (e.g. it just verified a merge produced 512 there) but the palette has
    no entry for that exponent yet, this samples the tile background at
    ``cell`` on a settled frame and returns a new :class:`Calibration`
    whose palette includes it -- so future color fallbacks (and the
    ``color_disagrees`` cross-check) know the tile without a human
    recalibrating.

    The sample is validated, not trusted: a color within
    :data:`LEARN_MIN_SEPARATION` of an entry with a DIFFERENT exponent
    (unsettled animation / wrong cell) raises :class:`BoardParseError`; a
    color that close to a SAME-exponent entry is already known and
    ``calibration`` is returned unchanged (nothing is written).

    Args:
        calibration: current calibration (not mutated).
        image: settled full-screen RGB frame showing the board.
        cell: 0..15 row-major cell index of the tile with the known value.
        exponent: tile exponent (>= 1) the engine verified at ``cell``.
        path: calibration JSON to update in place (other keys, e.g.
            ``state_anchors``, are preserved); ``None`` skips persistence.

    Returns:
        The calibration that knows the color (new or ``calibration`` itself).
    """
    _validate_image(image)
    if not 0 <= cell < BOARD_CELLS:
        raise ValueError(f"cell index must be 0..{BOARD_CELLS - 1}, got {cell}")
    if exponent < 1:
        raise ValueError(f"can only learn tile colors (exponent >= 1), got {exponent}")
    crop = cell_crop(image, calibration.cell_centers[cell], cell_half_size(calibration))
    background = tile_background_color(crop)
    rgb = cast(
        "tuple[int, int, int]", tuple(min(255, max(0, round(c))) for c in background)
    )
    nearest_rgb, nearest_exp = nearest_palette_entry(background, calibration.palette)
    nearest_dist = math.dist(background, nearest_rgb)
    if nearest_dist < LEARN_MIN_SEPARATION:
        if nearest_exp == exponent:
            _log.debug(
                "vision.palette.learn_duplicate", cell=cell, exponent=exponent, rgb=rgb
            )
            return calibration
        raise BoardParseError(
            f"cell {cell}: sampled rgb{rgb} for exponent {exponent} is only "
            f"{nearest_dist:.1f} away from the palette entry rgb{nearest_rgb} of "
            f"exponent {nearest_exp} -- unsettled frame or mislabeled cell, refusing "
            "to poison the palette"
        )
    learned = replace(calibration, palette=(*calibration.palette, (rgb, exponent)))
    if path is not None:
        payload: dict[str, object] = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                payload = data
        payload.update(learned.to_dict())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _log.info(
        "vision.palette.learned",
        cell=cell,
        exponent=exponent,
        rgb=rgb,
        nearest_distance=round(nearest_dist, 1),
        persisted=str(path) if path is not None else None,
    )
    return learned


def parse_board(
    image: Image,
    calibration: Calibration,
    glyphs: GlyphStore | None = None,
    *,
    lenient: bool = False,
) -> Board:
    """Classify all 16 cells of ``image`` into a row-major :data:`Board`.

    ``glyphs`` defaults to :meth:`GlyphStore.load_default` (packaged
    templates + ``data/glyphs/glyphs.json`` if present). With
    ``lenient=True`` a fully unknown or unsettled cell reads as exponent 0
    instead of raising (see :func:`parse_board_detailed`). See
    :func:`parse_board_detailed` for per-cell confidence/method detail.
    """
    return cast(
        Board,
        tuple(
            read.exponent
            for read in parse_board_detailed(image, calibration, glyphs, lenient=lenient)
        ),
    )

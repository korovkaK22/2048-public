"""Digit-glyph recognition: white tile digits -> characters, no OCR deps.

Why glyphs and not colors: the color palette only covers tiles that have
already been SEEN on a live board (2..64 today). The first time the bot
reaches 128 the face color is unknown and a color-only parser silently
misreads the board. The digits, however, reuse the same white font at every
tile value, so a tiny template matcher over the digit shapes generalizes to
any future tile as long as the individual digit characters are known.

Pipeline (all numpy, no cv2, no ML):

1. ``white_mask`` -- digit pixels are near-white on every tile face
   (min(R,G,B) >= :data:`WHITE_MIN`); tile faces and the board are not.
2. ``extract_components`` / ``digit_components`` -- 4-connected components
   of the mask; digits are well inside the crop, so components touching the
   crop border (the cat mascot's paw hanging over the board top) and
   undersized blobs are rejected.
3. ``normalize`` -- component bbox resampled (nearest) to a fixed
   :data:`GLYPH_SIZE` square + aspect ratio.
4. :meth:`GlyphStore.match` -- mean absolute difference against stored
   templates, smallest score wins; :func:`read_number` glues per-digit
   matches into the printed number, left to right.

Templates come from two sources, merged by :meth:`GlyphStore.load_default`:

* :mod:`bot2048.vision.glyph_templates` -- generated from real screenshots
  (``tests/vision/fixtures/``), committed with the code;
* ``data/glyphs/glyphs.json`` -- optional on-host store extended at runtime
  via ``bot2048 vision add-glyphs`` when a new digit first appears.

Measured on the reference device (1080x2400): digit glyphs are ~71-81 px
tall in a 160 px cell crop, start >= 30 px from the crop border, and every
digit of ``{1,2,3,4,6,8}`` matches its template with score < 0.06 while the
best WRONG digit scores > 0.30 (see tests/vision/test_glyphs.py).
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

__all__ = [
    "DEFAULT_GLYPHS_PATH",
    "GLYPH_SIZE",
    "MAX_DIGIT_SCORE",
    "WHITE_MIN",
    "Component",
    "Glyph",
    "GlyphError",
    "GlyphStore",
    "Match",
    "NumberRead",
    "digit_components",
    "extract_components",
    "normalize",
    "read_number",
    "white_mask",
]

#: Boolean glyph mask, shape (H, W).
Mask = npt.NDArray[np.bool_]
#: Normalized glyph raster, float32 in [0, 1], shape (GLYPH_SIZE, GLYPH_SIZE).
GlyphGrid = npt.NDArray[np.float32]

#: Side of the square raster every glyph is normalized to.
GLYPH_SIZE: int = 24

#: A pixel belongs to a digit glyph if min(R, G, B) >= WHITE_MIN.
#: Measured: digit cores are ~(250, 250, 250); the brightest tile face
#: channel-minimum is 58 (tile 2) / 13 (64-orange), so the gap is huge.
WHITE_MIN: int = 170

#: Worst acceptable per-digit match score. Cross-validated on all fixture
#: frames: correct digits score <= 0.077, the best WRONG digit scores
#: >= 0.169 -- 0.12 sits between the two populations.
MAX_DIGIT_SCORE: float = 0.12

#: Weight of the |aspect - template aspect| term in the match score.
ASPECT_WEIGHT: float = 0.25

#: Components shorter than this fraction of the crop height are noise or
#: partially occluded digits (measured digits: >= 0.44 of a 160 px crop).
MIN_HEIGHT_FRAC: float = 0.30

#: Components with fewer pixels than this fraction of the crop area are
#: specks (measured digits: >= 0.06 of a 160x160 crop).
MIN_AREA_FRAC: float = 0.004

#: Components whose bbox comes this close (px) to the crop border are
#: rejected: real digits sit well inside the cell, overhanging artwork
#: (the cat's paw) always touches the border.
EDGE_MARGIN: int = 2

#: On-host runtime-extensible glyph store (gitignored ``data/``).
DEFAULT_GLYPHS_PATH: Path = Path("data") / "glyphs" / "glyphs.json"


class GlyphError(ValueError):
    """Glyph store data is missing, malformed or inconsistent."""


def white_mask(crop: npt.NDArray[np.uint8]) -> Mask:
    """Boolean mask of near-white (digit) pixels of an RGB crop."""
    if crop.ndim != 3 or crop.shape[2] < 3:
        raise GlyphError(f"expected an (H, W, 3) RGB crop, got shape {crop.shape!r}")
    out: Mask = crop[:, :, :3].min(axis=2) >= WHITE_MIN
    return out


@dataclass(frozen=True, slots=True, eq=False)
class Component:
    """One 4-connected component of a white mask, cropped to its bbox."""

    x0: int  #: left edge of the bbox inside the source mask
    y0: int  #: top edge of the bbox inside the source mask
    mask: Mask  #: bbox-cropped boolean mask, shape (height, width)

    @property
    def width(self) -> int:
        return int(self.mask.shape[1])

    @property
    def height(self) -> int:
        return int(self.mask.shape[0])

    @property
    def area(self) -> int:
        return int(self.mask.sum())

    @property
    def aspect(self) -> float:
        """Width / height of the bbox."""
        return self.width / self.height


def extract_components(mask: Mask) -> tuple[Component, ...]:
    """4-connected components of ``mask``, sorted left to right (by x0)."""
    if mask.ndim != 2:
        raise GlyphError(f"expected a 2-D mask, got shape {mask.shape!r}")
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    comps: list[Component] = []
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys.tolist(), xs.tolist(), strict=True):
        if labels[sy, sx]:
            continue
        label = len(comps) + 1
        labels[sy, sx] = label
        queue: deque[tuple[int, int]] = deque([(sy, sx)])
        py: list[int] = []
        px: list[int] = []
        while queue:
            cy, cx = queue.popleft()
            py.append(cy)
            px.append(cx)
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not labels[ny, nx]:
                    labels[ny, nx] = label
                    queue.append((ny, nx))
        y0, y1 = min(py), max(py)
        x0, x1 = min(px), max(px)
        comps.append(Component(x0=x0, y0=y0, mask=mask[y0 : y1 + 1, x0 : x1 + 1].copy()))
    comps.sort(key=lambda c: c.x0)
    return tuple(comps)


def digit_components(mask: Mask) -> tuple[Component, ...]:
    """Components of ``mask`` that plausibly are digit glyphs.

    Rejects (in this order):

    * components touching the crop border (overhanging artwork -- the cat),
    * specks (area < :data:`MIN_AREA_FRAC` of the crop),
    * short components (height < :data:`MIN_HEIGHT_FRAC` of the crop, e.g.
      the visible sliver of a digit that is mostly under the cat's paw),
    * components much shorter than the tallest survivor (digits of one
      number are equally tall; mismatched heights mean debris).
    """
    h, w = mask.shape
    kept: list[Component] = []
    for comp in extract_components(mask):
        if (
            comp.x0 < EDGE_MARGIN
            or comp.y0 < EDGE_MARGIN
            or comp.x0 + comp.width > w - EDGE_MARGIN
            or comp.y0 + comp.height > h - EDGE_MARGIN
        ):
            continue
        if comp.area < MIN_AREA_FRAC * h * w:
            continue
        if comp.height < MIN_HEIGHT_FRAC * h:
            continue
        kept.append(comp)
    if not kept:
        return ()
    tallest = max(c.height for c in kept)
    return tuple(c for c in kept if c.height >= 0.72 * tallest)


def normalize(comp: Component) -> tuple[GlyphGrid, float]:
    """Resample a component to a (GLYPH_SIZE, GLYPH_SIZE) float raster.

    Nearest-neighbor sampling of the bbox (aspect is NOT preserved -- it is
    returned separately and scored by :meth:`GlyphStore.match`).
    """
    h, w = comp.mask.shape
    rows = np.minimum((np.arange(GLYPH_SIZE) + 0.5) * h / GLYPH_SIZE, h - 1).astype(np.intp)
    cols = np.minimum((np.arange(GLYPH_SIZE) + 0.5) * w / GLYPH_SIZE, w - 1).astype(np.intp)
    grid: GlyphGrid = comp.mask[np.ix_(rows, cols)].astype(np.float32)
    return grid, comp.aspect


@dataclass(frozen=True, slots=True, eq=False)
class Glyph:
    """One stored digit template."""

    digit: str  #: single character "0".."9"
    grid: GlyphGrid
    aspect: float

    def __post_init__(self) -> None:
        if len(self.digit) != 1 or not self.digit.isdigit():
            raise GlyphError(f"glyph digit must be a single 0-9 character, got {self.digit!r}")
        if self.grid.shape != (GLYPH_SIZE, GLYPH_SIZE):
            raise GlyphError(f"glyph grid must be {GLYPH_SIZE}x{GLYPH_SIZE}, got {self.grid.shape}")
        if not 0.05 <= self.aspect <= 5.0:
            raise GlyphError(f"implausible glyph aspect {self.aspect!r}")


@dataclass(frozen=True, slots=True)
class Match:
    """Best-template result for one component."""

    digit: str
    score: float  #: 0 = identical to a template; see MAX_DIGIT_SCORE

    @property
    def ok(self) -> bool:
        return self.score <= MAX_DIGIT_SCORE


@dataclass(frozen=True, slots=True)
class NumberRead:
    """Result of reading all digit components of one cell crop."""

    text: str | None  #: the printed number, or None if unreadable
    score: float  #: worst per-digit score (1.0 when there were no digits)
    matches: tuple[Match, ...]  #: per-component detail, left to right


class GlyphStore:
    """Immutable collection of digit templates + the matcher."""

    __slots__ = ("_glyphs",)

    def __init__(self, glyphs: tuple[Glyph, ...] = ()) -> None:
        self._glyphs = glyphs

    @property
    def glyphs(self) -> tuple[Glyph, ...]:
        return self._glyphs

    def __len__(self) -> int:
        return len(self._glyphs)

    def digits(self) -> frozenset[str]:
        """Set of digit characters this store can recognize."""
        return frozenset(g.digit for g in self._glyphs)

    def match(self, grid: GlyphGrid, aspect: float) -> Match:
        """Best template for a normalized component (smallest score)."""
        if not self._glyphs:
            raise GlyphError("glyph store is empty -- run `bot2048 vision add-glyphs`")
        best_digit = ""
        best_score = float("inf")
        for glyph in self._glyphs:
            score = float(np.abs(grid - glyph.grid).mean()) + ASPECT_WEIGHT * abs(
                aspect - glyph.aspect
            )
            if score < best_score:
                best_digit, best_score = glyph.digit, score
        return Match(digit=best_digit, score=best_score)

    def merged(self, other: GlyphStore) -> GlyphStore:
        """New store with templates of ``self`` followed by ``other``."""
        return GlyphStore(self._glyphs + other._glyphs)

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON representation ("01" strings, one per raster row)."""
        return {
            "version": 1,
            "size": GLYPH_SIZE,
            "glyphs": [
                {
                    "digit": g.digit,
                    "aspect": round(g.aspect, 4),
                    "rows": ["".join("1" if v >= 0.5 else "0" for v in row) for row in g.grid],
                }
                for g in self._glyphs
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GlyphStore:
        """Inverse of :meth:`to_dict`. Raises :class:`GlyphError` on bad input."""
        try:
            if int(data.get("size", GLYPH_SIZE)) != GLYPH_SIZE:
                raise GlyphError(
                    f"glyph store size {data.get('size')!r} != {GLYPH_SIZE} -- regenerate it"
                )
            glyphs = tuple(
                Glyph(
                    digit=str(g["digit"]),
                    grid=np.array(
                        [[1.0 if ch == "1" else 0.0 for ch in row] for row in g["rows"]],
                        dtype=np.float32,
                    ),
                    aspect=float(g["aspect"]),
                )
                for g in data["glyphs"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GlyphError(f"malformed glyph store data: {exc}") from exc
        return cls(glyphs)

    def save(self, path: Path = DEFAULT_GLYPHS_PATH) -> None:
        """Write the store as pretty-printed JSON (creates parent dirs)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> GlyphStore:
        """Load a JSON store. Raises :class:`GlyphError` if absent/bad."""
        if not path.is_file():
            raise GlyphError(f"glyph store not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GlyphError(f"cannot read glyph store {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise GlyphError(f"glyph store {path} must contain a JSON object")
        return cls.from_dict(data)

    @classmethod
    def packaged(cls) -> GlyphStore:
        """Templates generated from real screenshots, committed with the code."""
        from bot2048.vision.glyph_templates import TEMPLATES

        return cls.from_dict({"version": 1, "size": GLYPH_SIZE, "glyphs": list(TEMPLATES)})

    @classmethod
    def load_default(cls, extra_path: Path | None = DEFAULT_GLYPHS_PATH) -> GlyphStore:
        """Packaged templates + the optional on-host store (if the file exists)."""
        store = cls.packaged()
        if extra_path is not None and extra_path.is_file():
            store = store.merged(cls.load(extra_path))
        return store


def read_number(mask: Mask, store: GlyphStore) -> NumberRead:
    """Read the printed number off a cell-crop white mask.

    Returns ``text=None`` when there are no digit-like components or any
    component matches its best template worse than :data:`MAX_DIGIT_SCORE`
    (the caller then falls back to color classification).
    """
    comps = digit_components(mask)
    if not comps:
        return NumberRead(text=None, score=1.0, matches=())
    matches = tuple(store.match(*normalize(c)) for c in comps)
    worst = max(m.score for m in matches)
    text = "".join(m.digit for m in matches) if all(m.ok for m in matches) else None
    return NumberRead(text=text, score=worst, matches=matches)

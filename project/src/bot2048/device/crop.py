"""Board-only scrcpy crop: geometry, calibration transform, env opt-in.

Why: scrcpy-server (v4.0 jar bundled in this repo) accepts a server-side
``crop=WIDTH:HEIGHT:X:Y`` argument (screen-space coordinates of the device's
natural orientation, applied BEFORE any ``max_size`` scaling; verified against
the bundled jar's ``parseCrop`` -- "Crop must contains 4 values separated by
colons"). Streaming only the 2048 board rectangle instead of the full
1080x2400 screen shrinks every H.264 frame to ~31% of the pixels, cutting
encode/transfer/decode work on the perception path.

The pieces here are pure geometry + config plumbing:

* :func:`crop_region_for_board` -- the crop rectangle from the calibrated
  ``grid_bbox`` (+ padding, aligned for the H.264 encoder).
* :func:`crop_calibration` -- a :class:`~bot2048.vision.calibration.Calibration`
  whose coordinates live in CROP space, so :func:`bot2048.vision.parser.parse_board`
  works on crop frames unchanged (its ``screen_size`` check now expects the
  crop size).
* :func:`crop_frame` -- the same crop applied in software to a full frame
  (fixtures / equivalence tests / an offline reference).
* :func:`anchors_outside_region` -- which state anchors a crop frame CANNOT
  see. On the reference device ``popup_close`` (y=718), ``new_game_btn``
  (y=2143) and the win-popup buttons (y=1638) all lie OUTSIDE the board
  rectangle, so ``detect_state`` is IMPOSSIBLE on crop frames by design.
* :func:`crop_enabled` / :data:`CROP_ENV_VAR` -- the ``BOT2048_SCRCPY_CROP=1``
  opt-in flag. NOTHING reads it on the live path yet; default behavior is
  byte-for-byte unchanged.

FAIL-CLOSED CONTRACT for any future runner integration (the runner is out of
scope for this module -- see the report):

1. Crop frames feed :func:`~bot2048.vision.parser.parse_board` with the
   calibration from :func:`crop_calibration` ONLY -- never ``detect_state``.
2. Every state decision (popups, not-in-game, win, AUTH) MUST come from a
   FULL frame (``device.screenshot()`` / the screencap fallback ladder),
   taken periodically AND whenever anything is off (board parse error,
   unexpected board diff, secure/black signals).
3. If no sufficiently fresh full-frame state check says ``GAME``, do NOT
   swipe -- re-probe with a full frame first.

The ``FLAG_SECURE`` black-stream detection in
:class:`~bot2048.device.scrcpy.ScrcpySource` stays valid under crop: the
compositor blanks the WHOLE stream, crop included, while a real board crop is
never all-black (the darkest palette entry is rgb(21, 21, 36)).
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

from bot2048.vision.calibration import Calibration, CalibrationError
from bot2048.vision.parser import DEFAULT_PATCH_RADIUS, Image, cell_half_size
from bot2048.vision.states import StateAnchors

__all__ = [
    "CROP_ALIGN",
    "CROP_ENV_VAR",
    "DEFAULT_CROP_PADDING",
    "CropRegion",
    "anchors_outside_region",
    "board_crop_from_env",
    "crop_calibration",
    "crop_enabled",
    "crop_frame",
    "crop_region_for_board",
]

#: Opt-in flag: ``BOT2048_SCRCPY_CROP=1`` (also ``true``/``yes``/``on``).
#: Read via :func:`crop_enabled` by callers that KNOW to pair the cropped
#: stream with :func:`crop_calibration`; nothing consumes it implicitly.
CROP_ENV_VAR = "BOT2048_SCRCPY_CROP"

#: Padding (px, screen space) added around ``grid_bbox`` before alignment:
#: margin for the tile digits, the cat paw draping over the top row, and
#: swipe-animation overshoot. Small on purpose -- the state anchors outside
#: the board are NOT meant to be covered (see the module docstring).
DEFAULT_CROP_PADDING: int = 16

#: Crop width/height are rounded UP to this multiple. H.264 4:2:0 encoding
#: needs even dimensions; multiples of 8 keep MediaCodec happy across
#: devices (scrcpy itself aligns sizes to 8).
CROP_ALIGN: int = 8


@dataclass(frozen=True, slots=True)
class CropRegion:
    """A crop rectangle in device screen space (natural orientation, px).

    ``x``/``y`` is the top-left corner on the FULL screen; ``width``/``height``
    is the streamed frame size. Dimensions must be even (H.264 4:2:0);
    :func:`crop_region_for_board` produces :data:`CROP_ALIGN`-aligned ones.
    """

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0:
            raise ValueError(f"crop offset must be >= 0, got ({self.x}, {self.y})")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"crop size must be > 0, got {self.width}x{self.height}")
        if self.width % 2 or self.height % 2:
            raise ValueError(f"crop size must be even, got {self.width}x{self.height}")

    @property
    def size(self) -> tuple[int, int]:
        """``(width, height)`` of the cropped frame."""
        return (self.width, self.height)

    @property
    def as_config(self) -> tuple[int, int, int, int]:
        """``(x, y, width, height)`` -- the ``ScrcpyConfig.crop`` value."""
        return (self.x, self.y, self.width, self.height)

    @property
    def server_arg(self) -> str:
        """The scrcpy-server argument value: ``WIDTH:HEIGHT:X:Y``."""
        return f"{self.width}:{self.height}:{self.x}:{self.y}"

    def contains(self, point: tuple[int, int], margin: int = 0) -> bool:
        """``True`` when ``point`` (full-screen px) plus ``margin`` fits inside."""
        px, py = point
        return (
            self.x + margin <= px < self.x + self.width - margin
            and self.y + margin <= py < self.y + self.height - margin
        )

    def to_crop(self, point: tuple[int, int]) -> tuple[int, int]:
        """Full-screen ``(x, y)`` -> crop-frame ``(x, y)`` (may fall outside)."""
        return (point[0] - self.x, point[1] - self.y)

    def to_full(self, point: tuple[int, int]) -> tuple[int, int]:
        """Crop-frame ``(x, y)`` -> full-screen ``(x, y)``."""
        return (point[0] + self.x, point[1] + self.y)


def crop_region_for_board(
    calibration: Calibration,
    *,
    padding: int = DEFAULT_CROP_PADDING,
    align: int = CROP_ALIGN,
) -> CropRegion:
    """The board crop rectangle for ``calibration`` (padded, aligned, clipped).

    ``grid_bbox`` grows by ``padding`` on every side, the size is rounded UP
    to a multiple of ``align`` (extra pixels split evenly), the offset is
    rounded to even, and everything is clamped to ``screen_size`` -- the
    padded board itself is never cut.

    Raises:
        CalibrationError: the padded board does not fit on the screen at all
            (degenerate calibration), or ``padding``/``align`` are invalid.
    """
    if padding < 0:
        raise CalibrationError(f"padding must be >= 0, got {padding}")
    if align < 2 or align % 2:
        raise CalibrationError(f"align must be an even value >= 2, got {align}")
    left, top, right, bottom = calibration.grid_bbox
    screen_w, screen_h = calibration.screen_size
    x0, y0 = max(left - padding, 0), max(top - padding, 0)
    x1, y1 = min(right + padding, screen_w), min(bottom + padding, screen_h)
    width = math.ceil((x1 - x0) / align) * align
    height = math.ceil((y1 - y0) / align) * align
    if width > screen_w or height > screen_h:
        raise CalibrationError(
            f"aligned board crop {width}x{height} exceeds the "
            f"{screen_w}x{screen_h} screen -- bad grid_bbox {calibration.grid_bbox!r}?"
        )
    x = min(max(x0 - (width - (x1 - x0)) // 2, 0), screen_w - width)
    y = min(max(y0 - (height - (y1 - y0)) // 2, 0), screen_h - height)
    x -= x % 2
    y -= y % 2
    return CropRegion(x=x, y=y, width=width, height=height)


def crop_calibration(calibration: Calibration, region: CropRegion) -> Calibration:
    """``calibration`` translated into crop-frame coordinates.

    The returned calibration parses CROP frames: ``grid_bbox`` and every
    cell center are shifted by ``(-region.x, -region.y)``, ``screen_size``
    becomes the crop size (so the parser's frame-size check now enforces
    crop frames), and the palette is shared unchanged -- colors do not move.

    Raises:
        CalibrationError: some cell's crop patch (center +/- the calibrated
            half-size) would stick out of ``region`` -- the crop would cut
            tile pixels and the parse could not match the full frame.
    """
    half = cell_half_size(calibration)
    outside = [
        i
        for i, center in enumerate(calibration.cell_centers)
        if not region.contains(center, margin=half)
    ]
    if outside:
        raise CalibrationError(
            f"cell centers {outside} (+/-{half}px crop patch) fall outside the "
            f"crop region {region!r} -- widen the padding"
        )
    left, top, right, bottom = calibration.grid_bbox
    return Calibration(
        grid_bbox=(
            max(left - region.x, 0),
            max(top - region.y, 0),
            min(right - region.x, region.width),
            min(bottom - region.y, region.height),
        ),
        cell_centers=tuple(region.to_crop(c) for c in calibration.cell_centers),
        palette=calibration.palette,
        screen_size=region.size,
    )


def crop_frame(image: Image, region: CropRegion) -> Image:
    """``region`` cut from a full frame in software (fixtures/reference path).

    Pixel-identical to what scrcpy-server streams for the same region (crop
    happens before encoding, and ``max_size=0`` means no scaling), so tests
    can validate crop-frame parsing against full-frame fixtures offline.

    Raises:
        ValueError: ``image`` is smaller than the region.
    """
    h, w = image.shape[0], image.shape[1]
    if region.x + region.width > w or region.y + region.height > h:
        raise ValueError(f"frame {w}x{h} is smaller than crop region {region!r}")
    return image[region.y : region.y + region.height, region.x : region.x + region.width]


def anchors_outside_region(
    anchors: StateAnchors,
    region: CropRegion,
    *,
    margin: int = DEFAULT_PATCH_RADIUS,
) -> tuple[str, ...]:
    """Names of state anchors whose probe patch is NOT inside ``region``.

    A non-empty result means ``detect_state`` CANNOT run on crop frames with
    these anchors (their pixels are simply not streamed) -- the caller must
    keep state detection on FULL frames. On the reference device this is
    always non-empty for the board crop (``popup_close``, ``new_game_btn``,
    ``win_btn_*`` live outside the board), which is exactly why the
    fail-closed contract in the module docstring exists.
    """
    return tuple(
        sorted(
            name
            for name, spec in anchors.anchors.items()
            if not region.contains(spec.xy, margin=margin)
        )
    )


def crop_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``True`` when the :data:`CROP_ENV_VAR` opt-in flag is set.

    Accepted truthy spellings: ``1``, ``true``, ``yes``, ``on`` (any case).
    Absent/empty/anything else -> ``False`` (the default path stays exactly
    as before).
    """
    source = os.environ if env is None else env
    return source.get(CROP_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def board_crop_from_env(
    calibration: Calibration, env: Mapping[str, str] | None = None
) -> CropRegion | None:
    """:func:`crop_region_for_board` when the env flag is set, else ``None``.

    The single-call helper for a future runner hook: ``None`` means "keep the
    full-frame pipeline untouched".
    """
    return crop_region_for_board(calibration) if crop_enabled(env) else None

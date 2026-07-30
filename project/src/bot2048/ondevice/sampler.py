"""Reference implementation of the on-device board sampler.

This is the *specification* the phone-side (Kotlin/NDK) sampler must reproduce
bit-for-bit. It deliberately reuses the exact color path of
:mod:`bot2048.vision.parser` -- :func:`~bot2048.vision.parser.cell_crop`,
:func:`~bot2048.vision.parser.tile_background_color`,
:func:`~bot2048.vision.parser.nearest_palette_entry` -- so the fast color-only
read can never silently drift from the host's authoritative digits+color parser.

The algorithm, per cell center (row-major), is dependency-light on purpose so it
ports to Java/C over a raw RGBA buffer (see docs/ondevice-vision-design.md for
the device-side pseudocode):

1. crop a square of half-size ``round(0.37 * column_pitch)`` around the center;
2. take the per-channel **median of the non-white pixels** (``min(R,G,B) < 170``;
   if under 5 % of the crop is non-white, use all pixels) -- immune to the white
   digit glyphs and the cat mascot's paw;
3. classify by **nearest calibrated palette color** (smallest RGB Euclidean
   distance); the matched exponent is the cell value (0 = empty);
4. if that nearest distance exceeds :data:`UNCERTAIN_MAX_DIST`, flag the cell
   *uncertain* so the host reconciles it with a full-frame digit read.

Color-only is faithful for every calibrated tile: on the reference frames a
settled tile sits within ~1.5 of its palette entry and the nearest *wrong*
entry is > 25 away (tightest real pair, 128 vs 256 orange, ~40). Its blind spot
is a tile whose exponent is **not in the palette yet** -- it snaps confidently
to the nearest known color -- which is exactly what the periodic host-side digit
control check exists to catch and auto-learn.
"""

from __future__ import annotations

import math

from bot2048.core.types import BOARD_CELLS
from bot2048.ondevice.packet import BoardPacket, PacketFlag
from bot2048.vision.calibration import Calibration
from bot2048.vision.parser import (
    Image,
    cell_crop,
    cell_half_size,
    nearest_palette_entry,
    tile_background_color,
)

__all__ = [
    "NOT_IN_GAME_MIN_UNCERTAIN",
    "NOT_IN_GAME_STREAK",
    "UNCERTAIN_MAX_DIST",
    "NotInGameDetector",
    "build_packet",
    "is_black_frame",
    "sample_board_reference",
    "sample_cell_exponents",
]

#: A cell whose nearest-palette RGB distance exceeds this is flagged
#: *uncertain* in the packet. Well clear of settled tiles (<= ~1.5 on the
#: reference frames) yet far below the tightest inter-palette gap (~40), so
#: only genuinely ambiguous cells -- animating, occluded, or an unknown tile
#: snapping to a wrong entry -- trip it. The host resolves those with a
#: full-frame digit read. Distinct from the parser's harder ``UNKNOWN`` gate
#: (60.0), which is where even the host refuses to guess.
UNCERTAIN_MAX_DIST: float = 20.0

#: Max channel value for a frame to count as (near) all-black -- a suspected
#: ``FLAG_SECURE`` window. Matches ``ScrcpyConfig.black_pixel_max``: H.264
#: limited-range rounding leaves true black a few counts above 0.
_BLACK_PIXEL_MAX: int = 8

#: A sample with at least this many *uncertain* cells stops looking like the
#: game board at all: on real boards the uncertain mask is 0 (settled) or a
#: few animating cells, while a popup / foreign screen / lockscreen paints
#: most cell centers with colors far from every palette entry. 8 = half the
#: board -- far above any animation transient, far below a real board.
NOT_IN_GAME_MIN_UNCERTAIN: int = 8

#: Consecutive uncertain-storm samples before ``NOT_IN_GAME`` latches. One
#: frame can be a mid-animation smear; three in a row (>= ~50 ms apart at
#: VSYNC pacing) is a screen that genuinely is not the board.
NOT_IN_GAME_STREAK: int = 3


class NotInGameDetector:
    """Latches ``NOT_IN_GAME`` after a streak of uncertain-storm samples.

    This is the *specification* of the on-device detector (the Kotlin service
    ports it verbatim): feed every sampled packet's ``uncertain`` mask into
    :meth:`update`; it returns ``True`` while the sampler should flag
    :attr:`~bot2048.ondevice.packet.PacketFlag.NOT_IN_GAME`. Black
    (``SECURE_SUSPECTED``) frames must NOT be fed -- they carry no color
    evidence either way and have their own flag.

    The device reacts to the ``False -> True`` edge by saving an incident
    JPEG locally and pushing an ``INCIDENT`` frame to the host; the flag then
    stays up until a sample looks like the board again (streak broken).
    """

    def __init__(
        self,
        min_uncertain: int = NOT_IN_GAME_MIN_UNCERTAIN,
        streak: int = NOT_IN_GAME_STREAK,
    ) -> None:
        if min_uncertain < 1 or streak < 1:
            raise ValueError("min_uncertain and streak must be >= 1")
        self._min_uncertain = min_uncertain
        self._streak_needed = streak
        self._streak = 0

    def update(self, uncertain: int) -> bool:
        """Feed one packet's 16-bit ``uncertain`` mask; return the flag state."""
        if uncertain.bit_count() >= self._min_uncertain:
            self._streak += 1
        else:
            self._streak = 0
        return self._streak >= self._streak_needed


def is_black_frame(image: Image, max_value: int = _BLACK_PIXEL_MAX) -> bool:
    """``True`` when every channel of every pixel is ``<= max_value``.

    A sustained run of these signals a ``FLAG_SECURE`` window (the auth wall),
    where on-device capture yields black instead of pixels; the host must then
    use the ``screencap`` 0-byte AUTH sensor, not the board bytes.
    """
    return bool(image.size) and int(image[:, :, :3].max()) <= max_value


def sample_cell_exponents(
    image: Image,
    calibration: Calibration,
) -> tuple[tuple[int, ...], tuple[float, ...]]:
    """Color-classify all 16 cells; return ``(exponents, nearest_distances)``.

    ``exponents`` is row-major (``Board`` layout); ``nearest_distances[i]`` is
    the RGB Euclidean distance from cell ``i``'s sampled background to its
    matched palette entry (drives the uncertain mask). No digit reading -- this
    is exactly the cheap math the phone runs.
    """
    half = cell_half_size(calibration)
    palette = calibration.palette
    exps: list[int] = []
    dists: list[float] = []
    for center in calibration.cell_centers:
        crop = cell_crop(image, center, half)
        background = tile_background_color(crop)
        (r, g, b), exponent = nearest_palette_entry(background, palette)
        exps.append(exponent)
        dists.append(math.dist(background, (r, g, b)))
    assert len(exps) == BOARD_CELLS
    return tuple(exps), tuple(dists)


def sample_board_reference(image: Image, calibration: Calibration) -> tuple[int, ...]:
    """Color-only board read (row-major exponents) -- the fast-path board."""
    exps, _ = sample_cell_exponents(image, calibration)
    return exps


def build_packet(
    image: Image,
    calibration: Calibration,
    *,
    seq: int,
    prev_board: tuple[int, ...] | None = None,
    device_ts_ms: int = 0,
    uncertain_max_dist: float = UNCERTAIN_MAX_DIST,
) -> BoardPacket:
    """Sample ``image`` into a :class:`BoardPacket` exactly as the phone would.

    Sets :attr:`~bot2048.ondevice.packet.PacketFlag.SECURE_SUSPECTED` (and a
    zeroed board) on an all-black frame; otherwise fills the board, the
    ``uncertain`` mask (cells past ``uncertain_max_dist``) and the
    ``BOARD_CHANGED`` flag (vs ``prev_board``, when given).

    Args:
        seq: device sample counter (wraps at 2**16).
        prev_board: the previous packet's board, to set ``BOARD_CHANGED``;
            ``None`` leaves the flag unset.
        device_ts_ms: device monotonic timestamp (low 32 bits) for latency.
        uncertain_max_dist: nearest-distance threshold for the uncertain mask.
    """
    seq &= 0xFFFF
    device_ts_ms &= 0xFFFFFFFF
    if is_black_frame(image):
        return BoardPacket(
            board=(0,) * BOARD_CELLS,
            seq=seq,
            uncertain=0,
            flags=PacketFlag.SECURE_SUSPECTED,
            device_ts_ms=device_ts_ms,
        )
    exps, dists = sample_cell_exponents(image, calibration)
    uncertain = 0
    for i, dist in enumerate(dists):
        if dist > uncertain_max_dist:
            uncertain |= 1 << i
    flags = PacketFlag.NONE
    if uncertain:
        flags |= PacketFlag.HAS_UNCERTAIN
    if prev_board is not None and tuple(prev_board) != exps:
        flags |= PacketFlag.BOARD_CHANGED
    return BoardPacket(
        board=exps,
        seq=seq,
        uncertain=uncertain,
        flags=flags,
        device_ts_ms=device_ts_ms,
    )

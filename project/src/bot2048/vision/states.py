"""Screen-state detection: what is currently on the phone screen?

The live loop must not blindly parse a board out of a popup or lockscreen.
:func:`detect_state` classifies a screenshot into a coarse
:class:`ScreenState` before any board parsing happens.

IMPORTANT (known app bug): the monobank clone may show a game-over popup,
but it may also KEEP RUNNING when no moves remain. Therefore game over is
**always** decided by the bot itself via ``core.engine.is_game_over`` on the
parsed board; :data:`ScreenState.GAME_OVER_POPUP` is only a screen-handling
hint (e.g. "press the popup button / 'New game' to restart"), never the
source of truth.

Anchors and thresholds are DATA, not code: they live in
``data/calibration.json`` under the ``state_anchors`` key (measured values
are recorded in :mod:`bot2048.vision.reference`). The detector logic below
only compares mean-patch colors against those calibrated anchors -- the same
machinery as the board parser.

States that cannot be detected yet (no reference frame captured):

* ``AUTH_REQUIRED`` -- monobank sometimes demands a re-login that blocks the
  whole app incl. the game. No screenshot of that screen exists yet, so it
  classifies as ``NOT_IN_GAME``/``UNKNOWN``; the supervisor applies the same
  taps-forbidden pause policy (this is a BANKING app -- the bot never taps
  around blindly and never automates the PIN).

``GAME_OVER_POPUP`` (the «Гру завершено» card) IS detected now, from measured
anchors -- but per the note above it is only a screen-handling hint: it lets
the live loop finalize the current game and (with autorestart) tap «Нова гра»,
never the source of truth for whether the board is dead.

Timer caveat (docs/device-ui-notes.md): the on-screen game timer only
repaints on a swipe, so any frame taken between moves carries a STALE timer.
Consumers must read {board, score, timer} from one frame captured right
after a move (post-settle) and keep authoritative time on their own
wall-clock; the OCR'd timer is for drift checks only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from bot2048.vision.calibration import Calibration, CalibrationError
from bot2048.vision.glyphs import digit_components, white_mask
from bot2048.vision.parser import (
    Image,
    cell_crop,
    cell_half_size,
    mean_patch_color,
    nearest_palette_entry,
    tile_background_color,
)

__all__ = [
    "GAME_OVER_POPUP_ANCHOR_SETS",
    "WIN_POPUP_ANCHOR_SETS",
    "AnchorSpec",
    "ScreenState",
    "StateAnchors",
    "StateDetection",
    "detect_state",
    "load_calibration_bundle",
]

#: High-confidence gate for the board-present branch of :func:`detect_state`:
#: at or above this board-match fraction the board itself is authoritative and
#: classifies :data:`ScreenState.GAME` even when the single-pixel
#: ``new_game_btn`` anchor does not match. A genuine fresh game whose
#: «Нова гра» button renders its dimmed ↻ loading variant puts that anchor
#: pixel on background (incident 20260712T071957Z_SCREEN_UNEXPECTED FATALed a
#: 100%-matching board as UNKNOWN and took the live run down). Safe because
#: every popup state is excluded by the earlier branches, and no popup or
#: incident frame on record measures anywhere near this: the win-popup-tall
#: frame peaks at 0.75 and the synthetic odd-cells regression pins 13/16
#: (0.8125) as UNKNOWN. With 1/16 cell granularity, 0.95 admits only a
#: perfect 16/16 board.
_BOARD_HIGH_CONF_FRAC: float = 0.95

#: Anchor SETS of the «Ви зібрали 2048!» win popup. ALL anchors of ANY ONE set
#: must match to emit :data:`ScreenState.WIN_POPUP_2048` (fail-open: a set whose
#: anchors are absent from ``state_anchors`` simply never fires, so an older
#: calibration keeps the old behavior -- the popup then classifies as
#: ``NOT_IN_GAME``, exactly as before it was measurable). Each set is the
#: sky-blue artwork card (cat holding a 2048 tile) + BOTH flanks of the orange
#: «Нова гра» button; that orange button sits on the SAME row as the game-over
#: popup's grayish-blue one, so the SKY-BLUE card is the discriminator vs
#: :data:`ScreenState.GAME_OVER_POPUP` (whose card is teal).
#:
#: The popup ships in two MEASURED vertical layouts (see docs/device-ui-notes):
#:   * ``*``      -- SHORT card: «Нова гра» only (button row ~y1638, BELOW the
#:                   board, so its flanks sit at x430/x650).
#:   * ``*_tall`` -- TALL card: an extra «Вийти» below «Нова гра» pushes the
#:                   whole card up (orange button row ~y1557). That row now
#:                   OVERLAPS the board area, where a 128 tile (~255,129,14) is
#:                   orange enough to spoof a button flank -- so the tall flanks
#:                   sit in board COLUMN GAPS (x323/x756), which stay dark board
#:                   background on any real board. This is the variant that
#:                   slipped through as UNKNOWN in incident 20260711T114002Z
#:                   (score 38 632) before it was calibrated.
WIN_POPUP_ANCHOR_SETS: tuple[tuple[str, ...], ...] = (
    ("win_card", "win_btn_left", "win_btn_right"),
    ("win_card_tall", "win_btn_left_tall", "win_btn_right_tall"),
)

#: Anchor SETS of the «Гру завершено» game-over popup. ALL anchors of ANY ONE
#: set must match to emit :data:`ScreenState.GAME_OVER_POPUP` (fail-open: a set
#: whose anchors are absent from ``state_anchors`` simply never fires, so an
#: older calibration keeps the old behavior). Each set is the teal artwork card
#: + BOTH flanks of the grayish-blue «Нова гра» button (its center holds the
#: white label); the «Нова гра» button sits on the SAME row as the win popup's
#: orange one, so the TEAL card is the discriminator -- the win popup's card is
#: white/sky-blue at that point.
#:
#: The popup ships in two MEASURED vertical layouts (see docs/device-ui-notes):
#:   * ``*``      -- SHORT card: «Нова гра» only (button row ~y1638).
#:   * ``*_tall`` -- TALL card: an extra «Вийти» below «Нова гра» pushes the
#:                   whole card up (teal ~y630, button row ~y1520). This is the
#:                   variant that slipped through as NOT_IN_GAME in incident
#:                   20260711T105055Z (score 29 892) before it was calibrated.
GAME_OVER_POPUP_ANCHOR_SETS: tuple[tuple[str, ...], ...] = (
    ("game_over_card", "game_over_btn_left", "game_over_btn_right"),
    ("game_over_card_tall", "game_over_btn_left_tall", "game_over_btn_right_tall"),
)


def load_calibration_bundle(path: Path) -> tuple[Calibration, StateAnchors]:
    """Load ``calibration.json`` including the ``state_anchors`` section.

    Raises :class:`CalibrationError` if the file or the ``state_anchors``
    key is missing -- run ``bot2048 vision calibrate`` to (re)generate it.
    """
    if not path.is_file():
        raise CalibrationError(
            f"calibration file not found: {path} -- run `bot2048 vision calibrate`"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError(f"cannot read {path}: {exc}") from exc
    if "state_anchors" not in data:
        raise CalibrationError(
            f"{path} has no 'state_anchors' section -- re-run `bot2048 vision calibrate`"
        )
    return Calibration.from_dict(data), StateAnchors.from_dict(data["state_anchors"])


class ScreenState(Enum):
    """Coarse classification of a screenshot."""

    GAME = "game"  # normal game screen, board parseable
    CONFIRM_NEW_GAME = "confirm_new_game"  # "start a new game?" confirmation popup
    GAME_OVER_POPUP = "game_over_popup"  # «Гру завершено» card (hint only; see module docs)
    WIN_POPUP_2048 = "win_popup_2048"  # «Ви зібрали 2048!» win popup (terminal: won game)
    AUTH_REQUIRED = "auth_required"  # monobank re-login wall; not detected yet
    NOT_IN_GAME = "not_in_game"  # screen is on, but no board and no known popup
    SCREEN_OFF = "screen_off"  # black frame -> wake needed
    UNKNOWN = "unknown"  # contradictory anchors (e.g. board without HUD)
    # Emitted ONLY by bot2048.device.supervisor when there is no frame at all
    # (screencap AND ui_dump both failed); detect_state never returns it.
    DEVICE_LOST = "device_lost"
    # Emitted ONLY by bot2048.runner.escalation.probe_screen: screencap is
    # 0-byte (the documented FLAG_SECURE signature, docs/device-ui-notes.md)
    # and uiautomator produced no hierarchy even after retries, yet nothing
    # proves the device is gone (the adb link ANSWERED the screencap; in APK
    # mode board packets may still be flowing). "A secure surface we could not
    # confirm": observation-only -- NEVER routed into a tapping recovery, the
    # supervisor treats it like AUTH/LOST (no safe tap -> no taps at all).
    # detect_state never returns it.
    SECURE_UNCONFIRMED = "secure_unconfirmed"


@dataclass(frozen=True, slots=True)
class AnchorSpec:
    """One calibrated probe point: patch at ``xy`` should be near ``rgb``."""

    xy: tuple[int, int]
    rgb: tuple[int, int, int]
    tol: float  # max Euclidean RGB distance to count as a match

    def matches(self, image: Image, radius: int = 4) -> bool:
        color = mean_patch_color(image, self.xy, radius)
        d2 = sum((c - r) ** 2 for c, r in zip(color, self.rgb, strict=True))
        return d2 <= self.tol * self.tol


@dataclass(frozen=True, slots=True)
class StateAnchors:
    """Calibrated anchor points + thresholds for :func:`detect_state`.

    Serialized as the ``state_anchors`` key of ``data/calibration.json``
    (written by ``bot2048 vision calibrate``); measured values documented in
    :data:`bot2048.vision.reference.STATE_ANCHORS_DICT`.
    """

    anchors: dict[str, AnchorSpec] = field(default_factory=dict)
    screen_off_max_lum: float = 6.0
    cell_match_tol: float = 30.0
    #: GAME needs at least this fraction of board-consistent cells. Real
    #: game frames measure 1.0 with the digit-aware match; non-game screens
    #: (bank home, popups) measure <= ~0.4 -- 0.75 leaves a safe margin so a
    #: couple of odd cells (animation, glare) never flip a game to
    #: NOT_IN_GAME while non-boards stay far below.
    board_match_min_frac: float = 0.75

    REQUIRED: tuple[str, ...] = ("confirm_yes", "popup_close", "new_game_btn")

    def __post_init__(self) -> None:
        missing = [n for n in self.REQUIRED if n not in self.anchors]
        if missing:
            raise CalibrationError(f"state_anchors missing required anchors: {missing}")

    # -- serialization -----------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateAnchors:
        try:
            anchors = {
                str(name): AnchorSpec(
                    xy=(int(a["xy"][0]), int(a["xy"][1])),
                    rgb=(int(a["rgb"][0]), int(a["rgb"][1]), int(a["rgb"][2])),
                    tol=float(a["tol"]),
                )
                for name, a in data["anchors"].items()
            }
            return cls(
                anchors=anchors,
                screen_off_max_lum=float(data.get("screen_off_max_lum", 6.0)),
                cell_match_tol=float(data.get("cell_match_tol", 30.0)),
                board_match_min_frac=float(data.get("board_match_min_frac", 0.75)),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise CalibrationError(f"malformed state_anchors data: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchors": {
                name: {"xy": list(a.xy), "rgb": list(a.rgb), "tol": a.tol}
                for name, a in self.anchors.items()
            },
            "screen_off_max_lum": self.screen_off_max_lum,
            "cell_match_tol": self.cell_match_tol,
            "board_match_min_frac": self.board_match_min_frac,
        }


@dataclass(frozen=True, slots=True)
class StateDetection:
    """Classification result with a human-readable justification."""

    state: ScreenState
    reason: str
    board_match_frac: float  # fraction of the 16 cells that look board-consistent


def _board_match_fraction(image: Image, calibration: Calibration, tol: float) -> float:
    """Fraction of cells that look board-consistent (empty-like OR tile-like).

    A cell counts when either independent signal fires:

    * its BACKGROUND color is within ``tol`` of some calibrated palette
      entry -- covers empty cells and every already-seen tile. Uses the
      digit/occlusion-immune median background of each cell crop
      (:func:`bot2048.vision.parser.tile_background_color`) -- cell centers
      are TRUE centers, where a naive mean patch would hit the white glyphs;
    * the crop contains digit-like white glyph components
      (:func:`bot2048.vision.glyphs.digit_components`) -- every non-empty
      tile prints its value in the same white font, so a tile whose face
      color is NOT in the palette yet (the first 128, 256, ...) still reads
      as board-consistent.

    STATE detection must never depend on palette membership: digit
    recognition decides tile VALUES, this function only decides "is there a
    board here?". Regression: on the first live run two unknown 128 tiles
    dropped a perfectly valid game frame to 14/16 = 0.875 and the run was
    aborted as ``NOT_IN_GAME`` (logs/incidents/20260710T172050Z_SCREEN_UNEXPECTED).
    """
    hits = 0
    half = cell_half_size(calibration)
    for center in calibration.cell_centers:
        crop = cell_crop(image, center, half)
        color = tile_background_color(crop)
        (r, g, b), _ = nearest_palette_entry(color, calibration.palette)
        d2 = (r - color[0]) ** 2 + (g - color[1]) ** 2 + (b - color[2]) ** 2
        if d2 <= tol * tol or digit_components(white_mask(crop)):
            hits += 1
    return hits / len(calibration.cell_centers)


def detect_state(
    image: Image,
    calibration: Calibration,
    anchors: StateAnchors,
) -> StateDetection:
    """Classify ``image`` into a :class:`ScreenState` (never raises on content).

    Decision order (first match wins):

    1. near-black frame                        -> ``SCREEN_OFF``
    2. white/sky card + orange «Нова гра» btn  -> ``WIN_POPUP_2048``
    3. teal card + gray «Нова гра» button      -> ``GAME_OVER_POPUP``
    4. blue confirm button + white close "x"   -> ``CONFIRM_NEW_GAME``
    5. board-consistent cells + "New game" btn -> ``GAME``
    6. board-consistent, button missing        -> ``UNKNOWN`` (partial overlay?)
    7. anything else                           -> ``NOT_IN_GAME``

    Both popup branches (2, 3) are checked BEFORE the board so a popup
    covering the grid is never parsed as a board. ``AUTH_REQUIRED`` is
    defined but not yet detectable (no measured anchors -- see module docs);
    it falls into branches 6/7, which the supervisor treats conservatively.
    """
    # 1. screen off: cheap downsampled luminance.
    lum = float(image[::16, ::16, :3].mean())
    if lum <= anchors.screen_off_max_lum:
        return StateDetection(ScreenState.SCREEN_OFF, f"mean lum {lum:.1f}", 0.0)

    # 2. «Ви зібрали 2048!» win popup: the sky-blue card + BOTH flanks of the
    #    orange «Нова гра» button must all match (three independent anchors; the
    #    confirm popup shares a white card but never the orange button, real
    #    boards match neither). The popup ships in two vertical layouts (short /
    #    tall); ANY ONE fully matching set emits the state. Anchors are optional:
    #    a set whose anchors are absent falls through to the old branches.
    for group in WIN_POPUP_ANCHOR_SETS:
        specs = [anchors.anchors.get(name) for name in group]
        if all(spec is not None and spec.matches(image) for spec in specs):
            return StateDetection(
                ScreenState.WIN_POPUP_2048,
                f"win anchors ({' + '.join(group)})",
                0.0,
            )

    # 3. «Гру завершено» game-over popup: the teal artwork card + BOTH flanks
    #    of the grayish-blue «Нова гра» button must all match. That button
    #    shares its screen row with the win popup's orange one, so the TEAL
    #    card is what separates the two states (a board matches neither). The
    #    popup ships in two vertical layouts (short / tall); ANY ONE fully
    #    matching set emits the state. Anchors are optional exactly like the
    #    win branch: a set whose anchors are absent falls through to the old
    #    branches (no false GAME_OVER).
    for group in GAME_OVER_POPUP_ANCHOR_SETS:
        specs = [anchors.anchors.get(name) for name in group]
        if all(spec is not None and spec.matches(image) for spec in specs):
            return StateDetection(
                ScreenState.GAME_OVER_POPUP,
                f"game_over anchors ({' + '.join(group)})",
                0.0,
            )

    # 4. confirmation popup: two independent anchors must both match.
    if anchors.anchors["confirm_yes"].matches(image) and anchors.anchors["popup_close"].matches(
        image
    ):
        return StateDetection(
            ScreenState.CONFIRM_NEW_GAME, "confirm_yes + popup_close anchors", 0.0
        )

    # 5/6. board present?
    frac = _board_match_fraction(image, calibration, anchors.cell_match_tol)
    if frac >= anchors.board_match_min_frac:
        if anchors.anchors["new_game_btn"].matches(image):
            return StateDetection(ScreenState.GAME, f"board {frac:.0%} + new_game_btn", frac)
        if frac >= _BOARD_HIGH_CONF_FRAC:
            # Near-perfect board: authoritative on its own. The «Нова гра»
            # button's dimmed ↻ loading variant can put the single
            # new_game_btn anchor pixel on background; all popup states were
            # already excluded above (see _BOARD_HIGH_CONF_FRAC).
            return StateDetection(
                ScreenState.GAME, f"board {frac:.0%} high-conf, new_game_btn dark", frac
            )
        return StateDetection(
            ScreenState.UNKNOWN, f"board {frac:.0%} but new_game_btn missing", frac
        )

    # 7. lit screen, no board, no known popup.
    return StateDetection(ScreenState.NOT_IN_GAME, f"board only {frac:.0%}", frac)

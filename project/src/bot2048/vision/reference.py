"""Reference-device measurements: geometry, palette and state anchors.

Everything in this module was MEASURED, not guessed, on the reference device
(1080x2400, package ``com.ftband.mono``) from:

* ``tmp/fixtures/s0_start..s4_down.png`` -- live screencaps (probe2/probe3):
  grid columns x-spans ``[121,309] [337,525] [554,742] [770,958]``,
  row centers ``y = 868, 1084, 1301, 1517``, cell size 189 px, pitch ~216.6;
* ``screens/*.jpg`` -- real game frames with tiles up to 64;
* ``tmp/ng1_dialog.png`` -- the "start a new game?" confirmation popup;
* ``tmp/ng2_fresh.png`` -- board right after confirming a new game.

Cell centers are TRUE centers
-----------------------------
The parser estimates a cell's background as the median of the NON-white
crop pixels (:func:`bot2048.vision.parser.tile_background_color`), which is
immune to the white digit glyph and the cat's paw -- so ``cell_centers``
are the true geometric centers (row-major, matching ``Board``), where the
digit glyphs the parser template-matches are centered too. (Historical
note: the first color-only parser sampled inset points at ``(xc-62, yc-62)``
to dodge the glyphs; the median estimator made that obsolete.)

The empty-cell background has a vertical gradient -- one palette entry with
exponent ``0`` per board row.

Tile 128 was measured from the first live run that reached it
(``logs/incidents/20260710T172050Z_SCREEN_UNEXPECTED/screenshot_after.png``,
score 1508, two 128 tiles). Tile 256 was measured from the second live run
(``tests/vision/fixtures/board_256.png``, the first 128+128 merge). Tiles
512+ have no measured face color yet (never reached on a live board; the
light-themed artwork of the confirm-new-game dialog does not count). Their
DIGITS are still readable via the glyph store, so the digits-first parser
handles them; the color-only fallback raises
:class:`~bot2048.vision.parser.BoardParseError` on such unknown colors
instead of guessing -- and the runner can extend the palette from an
engine-verified merge via
:func:`bot2048.vision.parser.learn_tile_color`. NOTE: 64, 128 and 256 are
all orange, ~30-40 RGB apart -- close for the color-only fallback, but
digits win whenever readable.
"""

from __future__ import annotations

from bot2048.vision.calibration import Calibration, PaletteEntry

#: True cell-center columns/rows on the reference device (info + geometry).
COL_CENTERS: tuple[int, int, int, int] = (216, 432, 648, 864)
ROW_CENTERS: tuple[int, int, int, int] = (868, 1084, 1301, 1517)

#: Dark board-panel bounding box (left, top, right, bottom), right/bottom
#: exclusive -- what :func:`bot2048.vision.grid.detect_grid` detects; the
#: geometry fractions in that module are derived from this 900 px square.
PANEL_BBOX: tuple[int, int, int, int] = (90, 741, 990, 1641)

#: Board bounding box (left, top, right, bottom), right/bottom exclusive.
#: Cell x-extents 121..958, y-extents 774..1611, plus the ~14 px half-gap.
GRID_BBOX: tuple[int, int, int, int] = (107, 760, 973, 1625)

#: Measured palette: representative RGB -> tile exponent (0 = empty).
#: Empty background is a vertical gradient -> one entry per row.
PALETTE: tuple[PaletteEntry, ...] = (
    ((21, 21, 36), 0),  # empty, row 0
    ((23, 22, 38), 0),  # empty, row 1
    ((26, 24, 40), 0),  # empty, row 2
    ((29, 25, 41), 0),  # empty, row 3
    ((58, 58, 72), 1),  # 2  -- dark gray
    ((70, 71, 109), 2),  # 4  -- slate gray-violet
    ((38, 131, 223), 3),  # 8  -- blue
    ((14, 170, 165), 4),  # 16 -- teal
    ((29, 174, 93), 5),  # 32 -- green
    ((254, 159, 13), 6),  # 64 -- orange
    # 128 -- vivid orange, measured on the live board of the 2026-07-10
    # incident frame (see module docs); only ~30 away from 64.
    ((255, 129, 14), 7),
    # 256 -- deep orange, measured on the settled live board of run 2
    # (tests/vision/fixtures/board_256.png, cell r3c0); ~41 away from 128.
    ((250, 92, 31), 8),
)


def reference_calibration() -> Calibration:
    """The measured reference-device :class:`Calibration` (see module docs)."""
    centers = tuple((x, y) for y in ROW_CENTERS for x in COL_CENTERS)
    return Calibration(
        grid_bbox=GRID_BBOX,
        cell_centers=centers,
        palette=PALETTE,
        screen_size=(1080, 2400),
    )


#: State-anchor measurements (see :mod:`bot2048.vision.states`), serialized
#: into ``data/calibration.json`` under the ``state_anchors`` key so the
#: detector logic stays free of hardcoded colors.
#:
#: Measured mean RGB of a 9x9 patch at the anchor point, per screen state:
#:   confirm_yes    (540, 1532): (153, 216, 255) on CONFIRM_NEW_GAME,
#:                               (18, 14, 29) during normal play
#:   popup_close    (173, 718):  (250, 251, 253) on CONFIRM_NEW_GAME
#:   new_game_btn   (540, 2143): (90, 83, 121) during normal play,
#:                               (39, 37, 52) under the popup dim overlay
STATE_ANCHORS_DICT: dict[str, object] = {
    "anchors": {
        "confirm_yes": {"xy": [540, 1532], "rgb": [153, 216, 255], "tol": 40},
        "popup_close": {"xy": [173, 718], "rgb": [250, 251, 253], "tol": 40},
        "new_game_btn": {"xy": [540, 2143], "rgb": [90, 83, 121], "tol": 30},
        # «Ви зібрали 2048!» win popup (WIN_POPUP_2048). Measured on both
        # captured incidents (logs/incidents/20260710T202230Z_SCREEN_UNEXPECTED*
        # and *T212752Z_SCREEN_UNEXPECTED*): the white card interior above the
        # cat picture, plus BOTH flanks of the orange «Нова гра» button (the
        # button center holds white label text, so the anchors sit left/right
        # of it). Popup frames measure distance <= 2 for the button flanks and
        # <= 1 for the card; every board frame / the confirm popup / other
        # incident screens measure >= 249 (buttons) and >= 351 (card, except
        # the confirm popup's own white card at ~7 -- which is why the button
        # anchors must ALSO match). All three must match to emit the state.
        "win_card": {"xy": [540, 880], "rgb": [252, 251, 250], "tol": 40},
        "win_btn_left": {"xy": [430, 1638], "rgb": [255, 118, 0], "tol": 40},
        "win_btn_right": {"xy": [650, 1638], "rgb": [255, 118, 0], "tol": 40},
        # «Ви зібрали 2048!» win popup, TALL layout (WIN_POPUP_2048). The SAME
        # popup, but with an extra «Вийти» button below «Нова гра», which pushes
        # the whole card UP: the sky-blue artwork (cat holding a purple 2048
        # tile) sits higher and the orange «Нова гра» button row moves to y~1557
        # (not y1638). Measured on tests/vision/fixtures/win_popup_tall.png
        # (incident 20260711T114002Z_SCREEN_UNEXPECTED/screenshot_after.png, the
        # forced-2048 win of the 38 632 live game): the short-layout anchors all
        # miss it (card y880 lands on a gray sun-ray at 164/164/164; button
        # y1638 in the dead maroon gap BELOW the orange button at ~133/53/18), so
        # it fell through to UNKNOWN ("board 75% but new_game_btn missing") ->
        # FATAL. This second set matches with distance <= ~3 on that frame.
        #
        # CRITICAL vs the short layout: the y~1557 button row now OVERLAPS the
        # board area, where a 128 tile (~255,129,14) is orange enough to spoof a
        # button flank at x430/x650 (board_256.png measures a teal 16 at 430 and
        # dark bg at 650). So the tall flanks sit in board COLUMN GAPS -- x323
        # (between cols 0/1) and x756 (between cols 2/3) -- which stay dark board
        # background (~19,14,30) on ANY real board, never orange. The sky-blue
        # card (no tile is light sky-blue; teal game-over card is ~50 away)
        # remains the discriminator vs GAME_OVER_POPUP. All three must match.
        "win_card_tall": {"xy": [300, 780], "rgb": [149, 201, 231], "tol": 40},
        "win_btn_left_tall": {"xy": [323, 1557], "rgb": [255, 119, 0], "tol": 40},
        "win_btn_right_tall": {"xy": [756, 1557], "rgb": [255, 119, 0], "tol": 40},
        # «Гру завершено» game-over popup (GAME_OVER_POPUP). Measured on
        # tests/vision/fixtures/game_over_popup.png (incident
        # 20260711T062654Z_SCREEN_UNEXPECTED-2/screenshot_after.png, the end of
        # the 25k live game): the TEAL artwork card (scattered-tiles + white
        # paw) plus BOTH flanks of the grayish-blue «Нова гра» button (its
        # center holds the white label, so the anchors sit left/right of it).
        # The popup frame measures distance 0 for all three; every board / the
        # win popup / the confirm popup measure >= 75 on the teal card and
        # >= 200 on the button flanks. All three must match to emit the state.
        # The «Нова гра» button shares the win popup's button row (~540,1638),
        # so the teal card is the discriminator vs WIN_POPUP_2048.
        "game_over_card": {"xy": [540, 700], "rgb": [104, 219, 215], "tol": 40},
        "game_over_btn_left": {"xy": [430, 1638], "rgb": [107, 145, 178], "tol": 40},
        "game_over_btn_right": {"xy": [650, 1638], "rgb": [107, 145, 178], "tol": 40},
        # «Гру завершено» game-over popup, TALL layout (GAME_OVER_POPUP). The
        # SAME popup, but with an extra «Вийти» button below «Нова гра», which
        # pushes the whole card UP: the teal artwork sits at y~630 and the
        # grayish-blue «Нова гра» button row at y~1520 (not y1638). Measured on
        # tests/vision/fixtures/game_over_popup_tall.png (incident
        # 20260711T105055Z_SCREEN_UNEXPECTED/screenshot_after.png, the natural
        # game-over of the 29 892 live game): the short-layout anchors above all
        # miss it (card y700 lands on the white paw, button y1638 in the dead
        # gap below the button), so it fell through to NOT_IN_GAME -> FATAL.
        # This second set matches with distance 0 on that frame; every board /
        # the win popup / the confirm popup / the short game-over frame miss it.
        "game_over_card_tall": {"xy": [540, 630], "rgb": [105, 220, 215], "tol": 40},
        "game_over_btn_left_tall": {"xy": [430, 1520], "rgb": [108, 147, 178], "tol": 40},
        "game_over_btn_right_tall": {"xy": [650, 1520], "rgb": [108, 147, 178], "tol": 40},
        # «Синхронізація… Очікуємо дані для наступного раунду» overlay
        # (SYNC_OVERLAY): the whole screen dims and a card with that text plus
        # a translucent light-gray «Вийти» button is drawn OVER the board.
        # Measured on tests/vision/fixtures/sync_overlay.png (incident
        # 20260711T204021Z, 2-line text, button rows ~y1290..1390) and
        # sync_overlay_tall.png (incident 20260712T192946Z, 3-line text, button
        # ~y1350..1450) plus 8 more i7 frames: the button is translucent, so it
        # takes the color of whatever tile sits under it -- EXCEPT over the
        # board's dark column gaps (x~324..340, ~532..556, ~736..756), where it
        # measures (142,141,144) in every frame. y1370 is the row both layouts
        # share. All ten frames measure distance 0 on all three anchors; on a
        # real board the gaps are dark (~19,14,30, distance ~200), on the
        # win/game-over/confirm cards they are white/sky/teal -- none of the
        # 457 frames on record matches even two of the three. ``sync_gap_left``
        # is the dimmed column gap just LEFT of the button edge (the button
        # starts at x~322): (9,7,16)..(10,7,16) on all ten overlay frames. It
        # does not separate the overlay from a board (dark there too) -- it
        # rejects a uniformly gray screen that would satisfy the three gray
        # anchors alone.
        "sync_btn_left": {"xy": [338, 1370], "rgb": [142, 141, 144], "tol": 40},
        "sync_btn_center": {"xy": [540, 1370], "rgb": [142, 141, 144], "tol": 40},
        "sync_btn_right": {"xy": [748, 1370], "rgb": [142, 141, 144], "tol": 40},
        "sync_gap_left": {"xy": [312, 1370], "rgb": [9, 7, 16], "tol": 30},
        # TODO(auth): measure the monobank re-login screen (AUTH_REQUIRED);
        # until then it classifies as NOT_IN_GAME/UNKNOWN, which the
        # supervisor treats with the same taps-forbidden pause policy.
    },
    "screen_off_max_lum": 6.0,
    "cell_match_tol": 30.0,
    # Real game frames measure 1.0 (palette hit OR readable digits per
    # cell); non-game screens measure <= ~0.4. 0.75 keeps a safe margin so
    # a couple of odd cells never flip a live game to NOT_IN_GAME (the
    # 2026-07-10 incident aborted a valid game at 14/16 = 0.875 vs 0.9).
    "board_match_min_frac": 0.75,
}

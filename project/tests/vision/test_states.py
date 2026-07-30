"""Screen-state detection and the calibration+anchors bundle."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from bot2048.vision.calibration import Calibration, CalibrationError
from bot2048.vision.cli import MissingImageBackendError
from bot2048.vision.states import (
    ScreenState,
    StateAnchors,
    detect_state,
    load_calibration_bundle,
)
from tests.imgcache import decode_fixture
from tests.vision.conftest import Image

#: Real live frame the supervisor wrongly aborted on: a valid game (score
#: 1508) with two 128 tiles whose face color was not in the palette, so the
#: old palette-only board match scored 14/16 = 0.875 < 0.9 -> NOT_IN_GAME.
INCIDENT_128_FRAME = (
    Path(__file__).parents[2]
    / "logs"
    / "incidents"
    / "20260710T172050Z_SCREEN_UNEXPECTED"
    / "screenshot_after.png"
)


def load_incident_frame() -> Image:
    """The incident screenshot as RGB; skips if unavailable in this checkout."""
    if not INCIDENT_128_FRAME.is_file():
        pytest.skip(f"incident frame not present: {INCIDENT_128_FRAME}")
    try:
        return decode_fixture(INCIDENT_128_FRAME)
    except MissingImageBackendError as exc:  # pragma: no cover - env-dependent
        pytest.skip(str(exc))

GAME_FIXTURES = [
    "board_256.png",
    "board_32.jpg",
    "board_a.png",
    "board_b.png",
    "board_c.png",
    "board_d_labeled.png",
    "board_fresh.png",
    "board_full.jpg",
    "board_hi16_64.jpg",
    "live_1.png",
    "live_2.png",
    "live_3.png",
    "state_e.png",
    # the frame right before the win popup appeared (incident
    # 20260710T212752Z_SCREEN_UNEXPECTED-2/screenshot_before.png): a
    # perfectly ordinary board, must NOT classify as WIN_POPUP_2048.
    "win_popup_before.png",
    # the last good board of the 25k game (incident
    # 20260711T062654Z_SCREEN_UNEXPECTED/screenshot_before.png): two pink
    # 1024 tiles, an empty top-left cell and the cat's paw occluding the top
    # row -- a real, high-value board that MUST still read as GAME.
    "game_over_before.png",
]

#: «Ви зібрали 2048!» captures. Two MEASURED vertical layouts of the same
#: popup: the SHORT card («Нова гра» only, incidents 20260710T212752Z and
#: 20260710T202230Z, different HUD scores) and the TALL card (extra «Вийти»
#: pushes the orange «Нова гра» up to y~1557, incident 20260711T114002Z, score
#: 38 632). The tall one slipped through as UNKNOWN -> FATAL before its own
#: anchor set was calibrated.
WIN_POPUP_FIXTURES = [
    "win_popup.png",
    "win_popup_old.png",
    "win_popup_tall.png",
]

#: «Гру завершено» game-over captures. Two MEASURED vertical layouts of the
#: same popup: the SHORT card (only «Нова гра», incident 20260711T062654Z) and
#: the TALL card (extra «Вийти» pushes «Нова гра» up, incident
#: 20260711T105055Z, score 29 892). The tall one slipped through as
#: NOT_IN_GAME -> FATAL before its own anchor set was calibrated.
GAME_OVER_POPUP_FIXTURES = [
    "game_over_popup.png",
    "game_over_popup_tall.png",
]


@pytest.mark.parametrize("name", GAME_FIXTURES)
def test_game_screens_classify_as_game(
    name: str,
    load_image: Callable[[str], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    detection = detect_state(load_image(name), calib, anchors)
    assert detection.state is ScreenState.GAME
    assert detection.board_match_frac >= anchors.board_match_min_frac


def test_incident_frame_with_unknown_tiles_is_game(
    calib: Calibration, anchors: StateAnchors
) -> None:
    """Regression: a valid board with never-before-seen tiles must be GAME.

    This exact frame was misclassified as NOT_IN_GAME (board_match_frac
    0.875) because the two 128 tiles were not in the color palette; state
    detection must not depend on palette membership.
    """
    detection = detect_state(load_incident_frame(), calib, anchors)
    assert detection.state is ScreenState.GAME
    assert detection.board_match_frac >= anchors.board_match_min_frac


def test_incident_frame_is_game_even_without_128_in_palette(
    calib: Calibration, anchors: StateAnchors
) -> None:
    """The core fix: GAME even when the palette does NOT know the 128 color."""
    stripped = Calibration(
        grid_bbox=calib.grid_bbox,
        cell_centers=calib.cell_centers,
        palette=tuple(entry for entry in calib.palette if entry[1] < 7),
        screen_size=calib.screen_size,
    )
    detection = detect_state(load_incident_frame(), stripped, anchors)
    assert detection.state is ScreenState.GAME
    assert detection.board_match_frac >= anchors.board_match_min_frac


@pytest.mark.parametrize("name", GAME_OVER_POPUP_FIXTURES)
def test_game_over_popup_detected(
    name: str,
    load_image: Callable[[str], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    """Both «Гру завершено» layouts classify as GAME_OVER_POPUP.

    Requirement (а): the popup must be recognized so the live loop can finalize
    the game and (with autorestart) tap «Нова гра» -- previously it fell into
    NOT_IN_GAME and only paused. Covers the SHORT card (incident
    20260711T062654Z) AND the TALL card (incident 20260711T105055Z, score
    29 892) whose shifted anchors slipped through as NOT_IN_GAME -> FATAL.
    """
    detection = detect_state(load_image(name), calib, anchors)
    assert detection.state is ScreenState.GAME_OVER_POPUP
    # the popup covers the grid, so the board must NOT read as valid
    assert detection.board_match_frac < anchors.board_match_min_frac


@pytest.mark.parametrize("name", GAME_OVER_POPUP_FIXTURES)
def test_game_over_popup_is_not_a_board(
    name: str,
    load_image: Callable[[str], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    """A «Гру завершено» game-over popup must NOT read as a board.

    Regression for the live games: these frames are now caught by the
    GAME_OVER_POPUP anchors (checked before the board branch). Even so, the
    teal card + scattered-tile artwork (with its own white digit glyphs) must
    never satisfy the GAME board check -- loosening the threshold to accept a
    popup as a board would let the bot blindly swipe a game-over screen.
    """
    detection = detect_state(load_image(name), calib, anchors)
    assert detection.state is not ScreenState.GAME
    assert detection.board_match_frac < anchors.board_match_min_frac


@pytest.mark.parametrize("name", GAME_OVER_POPUP_FIXTURES)
def test_game_over_popup_is_not_win(
    name: str,
    load_image: Callable[[str], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    """Requirement (в): the game-over popup must NOT be mistaken for a win.

    Both popups put a «Нова гра» button on the same screen row, so only the
    teal (game-over) vs white/sky-blue (win) card separates them.
    """
    detection = detect_state(load_image(name), calib, anchors)
    assert detection.state is not ScreenState.WIN_POPUP_2048


@pytest.mark.parametrize("name", WIN_POPUP_FIXTURES)
def test_win_popup_is_not_game_over(
    name: str,
    load_image: Callable[[str], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    """Requirement (в) reversed: a win popup must NOT read as GAME_OVER_POPUP."""
    detection = detect_state(load_image(name), calib, anchors)
    assert detection.state is not ScreenState.GAME_OVER_POPUP


@pytest.mark.parametrize("name", [*GAME_FIXTURES, "confirm_new_game.jpg"])
def test_no_old_fixture_classifies_as_game_over(
    name: str,
    load_image: Callable[[str], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    """Regression: adding the game-over anchors must not reclassify anything.

    Includes ``game_over_before.png`` -- the real last board of the 25k game
    (requirement (б)): it must stay GAME, never GAME_OVER_POPUP.
    """
    detection = detect_state(load_image(name), calib, anchors)
    assert detection.state is not ScreenState.GAME_OVER_POPUP


def test_game_over_anchors_are_optional(
    load_image: Callable[[str], Image], calib: Calibration, anchors: StateAnchors
) -> None:
    """A bundle without game-over anchors still loads; the popup then degrades
    to a non-GAME state instead of a false GAME_OVER_POPUP (fail-open)."""
    data: dict[str, Any] = anchors.to_dict()
    for key in (
        "game_over_card",
        "game_over_btn_left",
        "game_over_btn_right",
        "game_over_card_tall",
        "game_over_btn_left_tall",
        "game_over_btn_right_tall",
    ):
        del data["anchors"][key]
    stripped = StateAnchors.from_dict(data)
    for name in GAME_OVER_POPUP_FIXTURES:
        detection = detect_state(load_image(name), calib, stripped)
        assert detection.state is not ScreenState.GAME_OVER_POPUP
        assert detection.state is not ScreenState.GAME


def test_a_few_odd_cells_do_not_flip_a_board_off(
    paint_board: Callable[[Sequence[int]], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    """3 unknown-color digit-free cells (13/16 = 0.8125) stay above 0.75."""
    from bot2048.vision.parser import cell_half_size

    frame = paint_board((0,) * 16).copy()
    half = cell_half_size(calib)
    for cx, cy in calib.cell_centers[:3]:
        frame[cy - half : cy + half, cx - half : cx + half] = (128, 128, 128)
    detection = detect_state(frame, calib, anchors)
    # painted boards have no HUD -> UNKNOWN, but crucially not NOT_IN_GAME
    assert detection.state is ScreenState.UNKNOWN
    assert detection.board_match_frac == pytest.approx(13 / 16)


@pytest.mark.parametrize("name", WIN_POPUP_FIXTURES)
def test_win_popup_detected(
    name: str,
    load_image: Callable[[str], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    """The win popup classifies as WIN_POPUP_2048, never SCREEN_UNEXPECTED."""
    detection = detect_state(load_image(name), calib, anchors)
    assert detection.state is ScreenState.WIN_POPUP_2048
    # the popup covers the grid, so the board must NOT read as valid
    assert detection.board_match_frac < anchors.board_match_min_frac


@pytest.mark.parametrize("name", [*GAME_FIXTURES, "confirm_new_game.jpg"])
def test_no_old_fixture_classifies_as_win_popup(
    name: str,
    load_image: Callable[[str], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    """Regression: adding the win anchors must not reclassify anything."""
    detection = detect_state(load_image(name), calib, anchors)
    assert detection.state is not ScreenState.WIN_POPUP_2048


def test_confirm_popup_beats_win_popup_priority(
    load_image: Callable[[str], Image], calib: Calibration, anchors: StateAnchors
) -> None:
    """CONFIRM_NEW_GAME keeps its exact class (its card is also white)."""
    detection = detect_state(load_image("confirm_new_game.jpg"), calib, anchors)
    assert detection.state is ScreenState.CONFIRM_NEW_GAME


def test_win_anchors_are_optional(
    load_image: Callable[[str], Image], calib: Calibration, anchors: StateAnchors
) -> None:
    """A bundle without win anchors still loads; the popup then degrades to
    a non-GAME state instead of a false WIN_POPUP_2048."""
    data: dict[str, Any] = anchors.to_dict()
    for key in (
        "win_card",
        "win_btn_left",
        "win_btn_right",
        "win_card_tall",
        "win_btn_left_tall",
        "win_btn_right_tall",
    ):
        del data["anchors"][key]
    stripped = StateAnchors.from_dict(data)
    for name in WIN_POPUP_FIXTURES:
        detection = detect_state(load_image(name), calib, stripped)
        assert detection.state is not ScreenState.WIN_POPUP_2048
        assert detection.state is not ScreenState.GAME


def test_confirm_popup_detected(
    load_image: Callable[[str], Image], calib: Calibration, anchors: StateAnchors
) -> None:
    detection = detect_state(load_image("confirm_new_game.jpg"), calib, anchors)
    assert detection.state is ScreenState.CONFIRM_NEW_GAME
    assert detection.board_match_frac < anchors.board_match_min_frac


def test_black_frame_is_screen_off(calib: Calibration, anchors: StateAnchors) -> None:
    black = np.zeros((2400, 1080, 3), dtype=np.uint8)
    assert detect_state(black, calib, anchors).state is ScreenState.SCREEN_OFF


def test_gray_frame_is_not_in_game(calib: Calibration, anchors: StateAnchors) -> None:
    gray = np.full((2400, 1080, 3), 128, dtype=np.uint8)
    assert detect_state(gray, calib, anchors).state is ScreenState.NOT_IN_GAME


def test_board_without_hud_is_unknown(
    paint_board: Callable[[Sequence[int]], Image],
    calib: Calibration,
    anchors: StateAnchors,
) -> None:
    # A PERFECT (16/16) board is now authoritative even without the
    # new_game_btn pixel: the «Нова гра» button's dimmed ↻ loading variant
    # puts that anchor pixel on background (incident
    # 20260712T071957Z_SCREEN_UNEXPECTED FATALed a 100% board as UNKNOWN).
    # See _BOARD_HIGH_CONF_FRAC and tests/vision/test_states_highconf_board.py;
    # below the gate the contradictory-frame UNKNOWN still applies
    # (test_a_few_odd_cells_do_not_flip_a_board_off pins 13/16 -> UNKNOWN).
    detection = detect_state(paint_board((0,) * 16), calib, anchors)
    assert detection.state is ScreenState.GAME
    assert detection.board_match_frac == 1.0
    assert "high-conf" in detection.reason


def test_anchor_dict_round_trip(anchors: StateAnchors) -> None:
    assert StateAnchors.from_dict(anchors.to_dict()).to_dict() == anchors.to_dict()


def test_missing_required_anchor_raises(anchors: StateAnchors) -> None:
    data: dict[str, Any] = anchors.to_dict()
    del data["anchors"]["new_game_btn"]
    with pytest.raises(CalibrationError, match="new_game_btn"):
        StateAnchors.from_dict(data)


def test_malformed_anchor_data_raises() -> None:
    with pytest.raises(CalibrationError):
        StateAnchors.from_dict({"anchors": {"confirm_yes": {"xy": [1]}}})


def test_bundle_round_trip(tmp_path: Path, calib: Calibration, anchors: StateAnchors) -> None:
    payload = calib.to_dict()
    payload["state_anchors"] = anchors.to_dict()
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded_calib, loaded_anchors = load_calibration_bundle(path)
    assert loaded_calib.to_dict() == calib.to_dict()
    assert loaded_anchors.to_dict() == anchors.to_dict()


def test_bundle_without_anchors_raises(tmp_path: Path, calib: Calibration) -> None:
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(calib.to_dict()), encoding="utf-8")
    with pytest.raises(CalibrationError, match="state_anchors"):
        load_calibration_bundle(path)


def test_bundle_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError, match="not found"):
        load_calibration_bundle(tmp_path / "nope.json")

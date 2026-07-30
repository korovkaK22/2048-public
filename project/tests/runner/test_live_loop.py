"""Unit tests for the live loop: fake phone + real Supervisor, zero delays."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest

from bot2048.core.types import Direction, Spawn
from bot2048.device import ui_map
from bot2048.device.supervisor import FatalIncident
from bot2048.recording import GameEnded, GameStarted, ListRecorder, MovePlayed, TileSpawned
from bot2048.runner.live import (
    SETTLE_DELAY_DEFAULT_S,
    LiveConfig,
    _WinPopup2048,
    diff_spawn,
)
from bot2048.runner.live_config import _APK_LULL_POLL_S, SETTLE_DELAY_APK_S
from tests.runner.conftest import (
    DEAD_BOARD,
    FRESH_BOARD,
    NEARLY_DEAD_BOARD,
    PREDICTED_AFTER_RIGHT,
    FakePhone,
    ScriptedStrategy,
    make_harness,
)


def _events(recorder: ListRecorder, kind: type) -> list:
    return [e for e in recorder.events if isinstance(e, kind)]


def _alert_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# -- diff_spawn (the verification/measurement primitive) ------------------------


class TestDiffSpawn:
    def test_single_spawn_found(self) -> None:
        assert diff_spawn(PREDICTED_AFTER_RIGHT, DEAD_BOARD) == Spawn(cell=0, exponent=1)

    def test_identical_boards_is_none(self) -> None:
        assert diff_spawn(DEAD_BOARD, DEAD_BOARD) is None

    def test_changed_occupied_cell_is_none(self) -> None:
        parsed = (2, *DEAD_BOARD[1:])
        assert diff_spawn(DEAD_BOARD, parsed) is None

    def test_two_new_tiles_is_none(self) -> None:
        base = (0, 0, *DEAD_BOARD[2:])
        parsed = (1, 1, *DEAD_BOARD[2:])
        assert diff_spawn(base, parsed) is None


# -- the happy path --------------------------------------------------------------


class TestRecordsMoves:
    def test_one_move_to_game_over(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        h.runner.run()

        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == NEARLY_DEAD_BOARD
        assert started[0].kind == "live"
        assert started[0].strategy == "scripted"

        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 1
        assert moves[0].direction is Direction.RIGHT
        assert moves[0].board_before == NEARLY_DEAD_BOARD
        assert moves[0].board_after == PREDICTED_AFTER_RIGHT  # pre-spawn prediction

        spawns = [e for e in _events(h.recorder, TileSpawned) if e.ply >= 0]
        assert [s.spawn for s in spawns] == [Spawn(cell=0, exponent=1)]
        assert spawns[0].board_after == DEAD_BOARD

        ended = _events(h.recorder, GameEnded)
        assert len(ended) == 1
        assert ended[0].end_reason == "game_over"  # engine decision, not a popup
        assert ended[0].num_moves == 1
        assert ended[0].max_exponent == 2

        # exactly one swipe went to the phone; no taps (no restart needed)
        assert phone.swipes == [Direction.RIGHT]
        assert phone.taps == []
        assert _alert_lines(h.alerts_path) == []  # clean game: no alerts

    def test_initial_tiles_recorded_with_negative_plies(self, tmp_path: Path) -> None:
        phone = FakePhone(board=FRESH_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=LiveConfig(experiment_id="t", max_games=1, max_moves_per_game=0),
        )
        h.runner.run()
        initial = [e for e in _events(h.recorder, TileSpawned) if e.ply < 0]
        assert [(s.ply, s.spawn.cell, s.spawn.exponent) for s in initial] == [
            (-1, 0, 1),
            (-2, 15, 1),
        ]
        assert _events(h.recorder, GameEnded)[0].end_reason == "aborted"  # move cap

    def test_heartbeat_updated_per_move(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        h.runner.run()
        game_id = _events(h.recorder, GameStarted)[0].game_id
        assert (game_id, 0) in h.heartbeat.updates  # game start
        assert (game_id, 1) in h.heartbeat.updates  # after the move


# -- restart on game over ---------------------------------------------------------


class TestRestart:
    def test_restarts_via_calibrated_taps(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1), Spawn(cell=7, exponent=2)]),
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.LEFT]),
            config=LiveConfig(experiment_id="t", max_games=2, max_moves_per_game=1),
        )
        h.runner.run()

        # restart flow: NEW_GAME then CONFIRM_YES, both calibrated taps
        assert ui_map.NEW_GAME in phone.taps
        assert ui_map.CONFIRM_YES in phone.taps

        started = _events(h.recorder, GameStarted)
        assert len(started) == 2
        assert started[1].initial_board == FRESH_BOARD
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["game_over", "aborted"]
        # second game recorded its move + scripted spawn
        second_spawns = [
            e.spawn
            for e in _events(h.recorder, TileSpawned)
            if e.game_id == started[1].game_id and e.ply >= 0
        ]
        assert second_spawns == [Spawn(cell=7, exponent=2)]


# -- «Ви зібрали 2048!» win popup ---------------------------------------------------


class TestWinPopup:
    def test_mid_game_win_ends_cleanly_when_no_games_budgeted(self, tmp_path: Path) -> None:
        """Autorestart off (budget spent): finalize win_2048, no FATAL, no tap."""
        phone = FakePhone(board=NEARLY_DEAD_BOARD, win_after_swipes=1)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        h.runner.run()  # must NOT raise FatalIncident

        ended = _events(h.recorder, GameEnded)
        assert [e.end_reason for e in ended] == ["win_2048"]
        # The popup hid the winning swipe's after-board: the move was never
        # verified/recorded and the score is the honest lower bound
        # last-verified (0 here: the RIGHT swipe merges nothing) + 2048.
        assert ended[0].num_moves == 0
        assert ended[0].final_score == 2048
        assert _events(h.recorder, MovePlayed) == []

        # session over: the popup is left on screen, nothing is tapped
        assert phone.taps == []
        assert phone.state == "win"
        assert ("INFO", "WIN_2048") in [(lvl, code) for lvl, code, _ in h.notifications]
        assert _alert_lines(h.alerts_path) == []  # happy terminal state: no alerts

    def test_win_autorestart_taps_win_new_game_directly(self, tmp_path: Path) -> None:
        """More games budgeted: one tap on the orange «Нова гра», no confirm."""
        phone = FakePhone(board=NEARLY_DEAD_BOARD, win_after_swipes=1)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.LEFT]),
            config=LiveConfig(experiment_id="t", max_games=2, max_moves_per_game=1),
        )
        h.runner.run()

        # restart is the win popup's own button -- NOT the NEW_GAME->CONFIRM flow
        assert ui_map.WIN_NEW_GAME in phone.taps
        assert ui_map.NEW_GAME not in phone.taps
        assert ui_map.CONFIRM_YES not in phone.taps

        started = _events(h.recorder, GameStarted)
        assert len(started) == 2
        assert started[1].initial_board == FRESH_BOARD  # a genuinely new game
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["win_2048", "aborted"]  # second game hits the move cap
        assert _alert_lines(h.alerts_path) == []

    def test_stale_win_popup_at_session_start_is_tapped_through(self, tmp_path: Path) -> None:
        """A leftover popup from a previous session: no game of ours to finalize."""
        phone = FakePhone(board=NEARLY_DEAD_BOARD, state="win")
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=LiveConfig(experiment_id="t", max_games=1, max_moves_per_game=0),
        )
        h.runner.run()

        assert phone.taps == [ui_map.WIN_NEW_GAME]
        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == FRESH_BOARD
        # the stale popup produced no win_2048 record -- it was not our game
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["aborted"]  # move cap 0, as configured


# -- «Гру завершено» game-over popup ------------------------------------------------


class TestGameOverPopup:
    def test_game_over_popup_ends_cleanly_when_no_games_budgeted(
        self, tmp_path: Path
    ) -> None:
        """Autorestart off (budget spent): finalize game_over, no FATAL, no tap."""
        # FRESH_BOARD is NOT dead, so the engine never calls game over first:
        # only the popup ends this game (the path under test).
        phone = FakePhone(board=FRESH_BOARD, game_over_after_swipes=1)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        h.runner.run()  # must NOT raise FatalIncident

        ended = _events(h.recorder, GameEnded)
        assert [e.end_reason for e in ended] == ["game_over"]
        # The popup hid the fatal swipe's after-board: the move was never
        # verified/recorded (the board is finalized as it stood before it).
        assert ended[0].num_moves == 0
        assert _events(h.recorder, MovePlayed) == []

        # session over: the popup is left on screen, nothing is tapped
        assert phone.taps == []
        assert phone.state == "gameover"
        # game_over is the NATURAL end of a game -> no ERROR/FATAL alerts
        assert _alert_lines(h.alerts_path) == []

    def test_game_over_autorestart_taps_new_game_directly(self, tmp_path: Path) -> None:
        """More games budgeted: one tap on «Нова гра» (same point as the win popup)."""
        phone = FakePhone(board=FRESH_BOARD, game_over_after_swipes=1)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.LEFT]),
            config=LiveConfig(experiment_id="t", max_games=2, max_moves_per_game=1),
        )
        h.runner.run()

        # restart is the popup's own «Нова гра» -- NOT the NEW_GAME->CONFIRM flow
        assert ui_map.WIN_NEW_GAME in phone.taps
        assert ui_map.NEW_GAME not in phone.taps
        assert ui_map.CONFIRM_YES not in phone.taps

        started = _events(h.recorder, GameStarted)
        assert len(started) == 2
        assert started[1].initial_board == FRESH_BOARD  # a genuinely new game
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["game_over", "aborted"]  # second game hits the move cap
        assert _alert_lines(h.alerts_path) == []

    def test_stale_game_over_popup_at_session_start_is_tapped_through(
        self, tmp_path: Path
    ) -> None:
        """A leftover popup from a previous session: no game of ours to finalize."""
        phone = FakePhone(board=FRESH_BOARD, state="gameover")
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=LiveConfig(experiment_id="t", max_games=1, max_moves_per_game=0),
        )
        h.runner.run()

        assert phone.taps == [ui_map.WIN_NEW_GAME]
        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == FRESH_BOARD
        # the stale popup produced no game_over record -- it was not our game
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["aborted"]  # move cap 0, as configured


# -- auth wall ---------------------------------------------------------------------


class TestAuthPause:
    def test_auth_pauses_input_and_escalates_fatal(self, tmp_path: Path) -> None:
        """No auto-login: input stops at the wall, AUTH_REQUIRED goes FATAL."""
        phone = FakePhone(board=NEARLY_DEAD_BOARD, auth_after_swipes=1)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        with pytest.raises(FatalIncident) as exc_info:
            h.runner.run()
        assert exc_info.value.alert.code.value == "AUTH_REQUIRED"

        # the swipe that hit the wall was the LAST input; zero taps ever
        assert phone.swipes == [Direction.RIGHT]
        assert phone.taps == []
        # the aborted game is finalized before the FATAL escalates: the summary
        # row never stays half-written, and it names the actual cause (the bank
        # auth wall) instead of a blanket 'error' (§5)
        ended = _events(h.recorder, GameEnded)
        assert [e.end_reason for e in ended] == ["auth_required"]
        assert ended[0].num_moves == 0  # the wall swipe was never verified
        fatal = [a for a in _alert_lines(h.alerts_path) if a["level"] == "FATAL"]
        assert len(fatal) == 1 and fatal[0]["code"] == "AUTH_REQUIRED"

    def test_auto_login_reset_starts_new_recorded_game(self, tmp_path: Path) -> None:
        """Opt-in PIN entry; a changed post-auth board = app regenerated the game."""
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            auth_after_swipes=1,
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=1, auto_login=True),
            auto_pin="1234",
        )
        h.runner.run()

        # PIN typed only at calibrated pin-pad points
        pin_points = set(ui_map.PIN_KEYS.values())
        assert len([t for t in phone.taps if t in pin_points]) == 4
        # old game interrupted, new game adopted from full re-recognition
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons[0] == "interrupted"
        started = _events(h.recorder, GameStarted)
        assert len(started) == 2
        assert started[1].initial_board == DEAD_BOARD  # what the screen showed
        assert reasons[1] == "game_over"  # DEAD_BOARD has no moves


class TestRestartAuthWall:
    """Restart flow that routes «Нова гра» through re-auth (expired token)."""

    def test_auto_login_clears_wall_seamlessly(self, tmp_path: Path) -> None:
        """auto-login clears the restart auth wall: no FATAL, no wasted budget."""
        phone = FakePhone(
            board=FRESH_BOARD,
            game_over_after_swipes=1,  # game #1 ends via «Гру завершено» popup
            new_game_needs_auth=True,  # its «Нова гра» tap lands on the PIN pad
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.LEFT]),
            config=LiveConfig(
                experiment_id="t", max_games=2, max_moves_per_game=1, auto_login=True
            ),
            auto_pin="1234",
        )
        h.runner.run()  # must NOT raise FatalIncident

        # restart tapped «Нова гра», hit the wall, and the PIN was typed at the
        # calibrated pin-pad points only (no blind exploration)
        assert ui_map.WIN_NEW_GAME in phone.taps
        pin_points = set(ui_map.PIN_KEYS.values())
        assert len([t for t in phone.taps if t in pin_points]) == 4

        # both games ran to completion; the second is the fresh post-auth board
        started = _events(h.recorder, GameStarted)
        assert len(started) == 2
        assert started[1].initial_board == FRESH_BOARD
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["game_over", "aborted"]

        # the auth wall on restart is NOT an incident: no FATAL, no
        # GAME_OVER_UNEXPECTED, and the session-recovery budget is untouched
        alerts = _alert_lines(h.alerts_path)
        assert [a for a in alerts if a["level"] == "FATAL"] == []
        assert [a for a in alerts if a["code"] == "GAME_OVER_UNEXPECTED"] == []
        assert len(h.runner._recoveries) == 0

    def test_no_auto_login_still_fatals(self, tmp_path: Path) -> None:
        """auto-login off: the restart wall is the SAME AUTH_REQUIRED FATAL as before."""
        phone = FakePhone(
            board=FRESH_BOARD,
            game_over_after_swipes=1,
            new_game_needs_auth=True,
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.LEFT]),
            config=LiveConfig(experiment_id="t", max_games=2, max_moves_per_game=1),
        )
        with pytest.raises(FatalIncident) as exc_info:
            h.runner.run()
        assert exc_info.value.alert.code.value == "AUTH_REQUIRED"

        # «Нова гра» landed (that surfaced the wall) but zero taps on the pin pad
        assert ui_map.WIN_NEW_GAME in phone.taps
        pin_points = set(ui_map.PIN_KEYS.values())
        assert [t for t in phone.taps if t in pin_points] == []

        # game #1 finished cleanly; only game #1 recorded (FATAL before game #2)
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["game_over"]
        fatal = [a for a in _alert_lines(h.alerts_path) if a["level"] == "FATAL"]
        assert len(fatal) == 1 and fatal[0]["code"] == "AUTH_REQUIRED"
        # AUTH_REQUIRED is non-recoverable: the FATAL is not charged to the budget
        assert len(h.runner._recoveries) == 0


class TestAuthTerminalPopup:
    """A win / game-over popup hiding UNDER the auth wall must RESTART, not FATAL.

    Regression for the live incident: start -> auth_required -> auto-login typed
    the PIN -> a win popup sat behind it -> ``_handle_auth`` expected GAME, saw a
    non-GAME popup and FATALed 'auto-login did not reach GAME'. The restart
    ability existed but was reachable ONLY from the game loop; now every path
    funnels a KNOWN terminal popup into the same auth-tolerant restart.
    """

    def test_auth_recovery_on_win_popup_restarts_no_fatal(self, tmp_path: Path) -> None:
        """(a) Mid-game auth wall clears onto a win popup -> restart, never FATAL."""
        phone = FakePhone(
            board=FRESH_BOARD,
            auth_after_swipes=1,  # the first move's verify hits the wall
            pin_reveals_state="win",  # ...and the PIN reveals a «Ви зібрали 2048!» card
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(
                experiment_id="t",
                max_games=2,
                max_moves_per_game=5,
                auto_login=True,
                auth_popup_restart_limit=5,  # decouple from the loop-guard here
            ),
            auto_pin="1234",
        )
        h.runner.run()  # must NOT raise FatalIncident

        # PIN typed at calibrated pad points; the popup's «Нова гра» restarted us
        pin_points = set(ui_map.PIN_KEYS.values())
        assert len([t for t in phone.taps if t in pin_points]) >= 4
        assert ui_map.WIN_NEW_GAME in phone.taps
        # the popup-under-auth is finalized as a WON game (as the game loop does),
        # then a fresh game is started -- reached GAME, no FATAL
        started = _events(h.recorder, GameStarted)
        assert len(started) == 2
        assert started[1].initial_board == FRESH_BOARD
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["win_2048", "win_2048"]
        assert [a for a in _alert_lines(h.alerts_path) if a["level"] == "FATAL"] == []

    def test_auth_recovery_on_game_over_popup_restarts_no_fatal(
        self, tmp_path: Path
    ) -> None:
        """(b) Mid-game auth wall clears onto a «Гру завершено» popup -> restart."""
        phone = FakePhone(
            board=FRESH_BOARD,
            auth_after_swipes=1,
            pin_reveals_state="gameover",
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(
                experiment_id="t",
                max_games=2,
                max_moves_per_game=5,
                auto_login=True,
                auth_popup_restart_limit=5,
            ),
            auto_pin="1234",
        )
        h.runner.run()  # must NOT raise FatalIncident

        assert ui_map.WIN_NEW_GAME in phone.taps  # game-over popup shares the point
        started = _events(h.recorder, GameStarted)
        assert len(started) == 2
        assert started[1].initial_board == FRESH_BOARD
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["game_over", "game_over"]
        assert [a for a in _alert_lines(h.alerts_path) if a["level"] == "FATAL"] == []

    def test_startup_acquire_on_popup_under_auth_restarts_no_fatal(
        self, tmp_path: Path
    ) -> None:
        """(c) Session start behind the auth wall reveals a popup -> restart to GAME."""
        phone = FakePhone(
            board=FRESH_BOARD,
            state="auth",  # the very first capture hits the wall
            pin_reveals_state="win",  # ...and a win popup sits behind it
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=LiveConfig(
                experiment_id="t", max_games=1, max_moves_per_game=0, auto_login=True
            ),
            auto_pin="1234",
        )
        h.runner.run()  # must NOT raise FatalIncident

        pin_points = set(ui_map.PIN_KEYS.values())
        assert len([t for t in phone.taps if t in pin_points]) == 4
        assert ui_map.WIN_NEW_GAME in phone.taps
        # a genuinely fresh game was acquired (not our win to claim at startup)
        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == FRESH_BOARD
        assert _events(h.recorder, GameEnded)[0].end_reason == "aborted"  # move cap 0
        assert [a for a in _alert_lines(h.alerts_path) if a["level"] == "FATAL"] == []

    def test_unknown_after_pin_still_fatals(self, tmp_path: Path) -> None:
        """(d) A non-popup non-GAME screen after the PIN keeps the fail-closed FATAL."""
        phone = FakePhone(
            board=FRESH_BOARD,
            state="auth",
            pin_reveals_state="broken",  # UNKNOWN screen -> genuinely unsafe
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=LiveConfig(experiment_id="t", max_games=1, auto_login=True),
            auto_pin="1234",
        )
        with pytest.raises(FatalIncident) as exc_info:
            h.runner.run()
        assert exc_info.value.alert.code.value == "AUTH_REQUIRED"

    def test_terminal_popup_restart_loop_guard_fatals(self, tmp_path: Path) -> None:
        """(e) popup->restart->popup with no move between -> FATAL, never a live-lock."""
        phone = FakePhone(board=FRESH_BOARD, state="auth", pin_reveals_state="win")
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=LiveConfig(
                experiment_id="t", auto_login=True, auth_popup_restart_limit=2
            ),
            auto_pin="1234",
        )
        r = h.runner
        # Each auth->popup (simulating a fresh game that instantly re-hits the
        # wall) surfaces the popup exception the restart path handles. No move
        # commits, so the guard keeps climbing.
        for _ in range(2):  # limit=2: the first two are tolerated
            phone.state = "auth"
            with pytest.raises(_WinPopup2048):
                r._capture_game_frame(None)
        # One more with nothing progressed -> fail closed instead of looping.
        phone.state = "auth"
        with pytest.raises(FatalIncident) as exc_info:
            r._capture_game_frame(None)
        assert exc_info.value.alert.code.value == "SCREEN_UNEXPECTED"
        fatal = [a for a in _alert_lines(h.alerts_path) if a["level"] == "FATAL"]
        assert len(fatal) == 1 and fatal[0]["code"] == "SCREEN_UNEXPECTED"


# -- unchanged board / vision mismatch ----------------------------------------------


class TestMoveRejected:
    def test_retries_then_error_alert_and_abort(self, tmp_path: Path) -> None:
        phone = FakePhone(board=NEARLY_DEAD_BOARD, reject_swipes=99)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(
                experiment_id="t", max_games=1, swipe_retries=2, parse_retries=2
            ),
        )
        h.runner.run()

        assert phone.swipes == [Direction.RIGHT] * 3  # 1 + swipe_retries
        errors = [a for a in _alert_lines(h.alerts_path) if a["level"] == "ERROR"]
        assert len(errors) == 1
        assert errors[0]["code"] == "MOVE_REJECTED"
        assert errors[0]["attempt"] == 3
        assert Path(tmp_path, "incidents").exists()  # bundle written
        assert _events(h.recorder, GameEnded)[0].end_reason == "move_rejected"
        assert _events(h.recorder, MovePlayed) == []  # unverified move never recorded


class TestVisionMismatch:
    def test_persistent_mismatch_aborts_without_guessing(self, tmp_path: Path) -> None:
        corrupt = (3, 3, 3, 3, *NEARLY_DEAD_BOARD[4:])
        phone = FakePhone(board=NEARLY_DEAD_BOARD, corrupt_to=corrupt)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=1, parse_retries=2),
        )
        h.runner.run()

        assert phone.swipes == [Direction.RIGHT]  # no blind re-swipes on mismatch
        errors = [a for a in _alert_lines(h.alerts_path) if a["level"] == "ERROR"]
        assert [a["code"] for a in errors] == ["VISION_MISMATCH"]
        assert _events(h.recorder, GameEnded)[0].end_reason == "vision_mismatch"
        assert _events(h.recorder, MovePlayed) == []
        assert _events(h.recorder, TileSpawned)[-1].ply < 0  # no guessed spawn


# -- wait-for-stable-frame (mid-merge animation must not trip VISION_MISMATCH) -------

#: Two distinct "mid-merge" frames: neither pre-move nor predicted+spawn.
TRANSIENT_A = (0, 0, 2, 0, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)
TRANSIENT_B = (0, 0, 0, 2, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)


class TestStableFrame:
    def test_animation_frames_settle_then_verify(self, tmp_path: Path) -> None:
        """Successive captures: animating, animating, settled -> the loop waits
        for two identical consecutive parses and verifies without a false
        VISION_MISMATCH (live run 2 regression)."""
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            animate_after_swipe=(TRANSIENT_A, TRANSIENT_B),
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, settle_max_wait_s=60.0),
        )
        h.runner.run()

        assert not phone.animate_reads  # the transient frames WERE seen...
        assert _alert_lines(h.alerts_path) == []  # ...and produced no alert
        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 1  # verified against the SETTLED board
        spawns = [e for e in _events(h.recorder, TileSpawned) if e.ply >= 0]
        assert [s.spawn for s in spawns] == [Spawn(cell=0, exponent=1)]
        assert spawns[0].board_after == DEAD_BOARD
        assert _events(h.recorder, GameEnded)[0].end_reason == "game_over"

    def test_genuinely_unchanged_board_still_move_rejected(self, tmp_path: Path) -> None:
        """A swipe that never registers (stable pre-move board) keeps the
        MOVE_REJECTED semantics even with stabilization on."""
        phone = FakePhone(board=NEARLY_DEAD_BOARD, reject_swipes=99)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(
                experiment_id="t",
                max_games=1,
                swipe_retries=1,
                parse_retries=2,
                settle_max_wait_s=60.0,
            ),
        )
        h.runner.run()
        assert phone.swipes == [Direction.RIGHT] * 2  # 1 + swipe_retries
        errors = [a for a in _alert_lines(h.alerts_path) if a["level"] == "ERROR"]
        assert [a["code"] for a in errors] == ["MOVE_REJECTED"]
        assert _events(h.recorder, MovePlayed) == []

    def test_settled_inconsistent_board_still_vision_mismatch(self, tmp_path: Path) -> None:
        """Animation frames followed by a STABLE but impossible board must
        still abort as VISION_MISMATCH -- stabilization only removes the
        false mismatches, never the real ones."""
        corrupt = (3, 3, 3, 3, *NEARLY_DEAD_BOARD[4:])
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            corrupt_to=corrupt,
            animate_after_swipe=(TRANSIENT_A,),
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(
                experiment_id="t", max_games=1, parse_retries=2, settle_max_wait_s=60.0
            ),
        )
        h.runner.run()
        assert not phone.animate_reads  # transient frame consumed while settling
        errors = [a for a in _alert_lines(h.alerts_path) if a["level"] == "ERROR"]
        assert [a["code"] for a in errors] == ["VISION_MISMATCH"]
        assert _events(h.recorder, GameEnded)[0].end_reason == "vision_mismatch"
        assert _events(h.recorder, MovePlayed) == []


# -- predict-gate (early spawn detection; the legacy settle stays as fallback) -------

#: PREDICTED_AFTER_RIGHT with an IMPOSSIBLE "spawn": an exponent-3 tile sliding
#: over the empty cell 0 while every other cell already matches the prediction.
FALSE_EARLY_FRAME = (3, *PREDICTED_AFTER_RIGHT[1:])


def _spy_in_game_settles(h) -> list:  # type: ignore[no-untyped-def]
    """Count :meth:`_settled_board` calls made DURING a game (state != None).

    The initial board acquisition legitimately stabilizes (state is None);
    any non-None entry means the slow legacy settle ran after a swipe.
    """
    calls: list[object] = []
    # The stable-frame settle now lives on the ObservationGate (R4). The
    # in-game verification path (observe_after_swipe_frames) calls it through
    # ``self.settled_board``, so the spy must shadow the GATE's method to see
    # those non-None calls; the initial acquisition still reaches it via the
    # runner delegator (state None), exactly as before the extraction.
    original = h.runner._gate.settled_board

    def spy(state, frame=None):  # type: ignore[no-untyped-def]
        calls.append(state)
        return original(state, frame)

    h.runner._gate.settled_board = spy  # type: ignore[method-assign]
    return calls


class TestPredictGate:
    def test_happy_path_accepts_first_good_frame_without_stabilization(
        self, tmp_path: Path
    ) -> None:
        """(а) The gate takes the first frame == prediction + one settled 2/4
        spawn and exits early: no post-swipe stable-frame settle at all."""
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        settles = _spy_in_game_settles(h)
        h.runner.run()

        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 1
        assert moves[0].board_after == PREDICTED_AFTER_RIGHT
        spawns = [e for e in _events(h.recorder, TileSpawned) if e.ply >= 0]
        assert [s.spawn for s in spawns] == [Spawn(cell=0, exponent=1)]
        assert spawns[0].board_after == DEAD_BOARD
        assert _alert_lines(h.alerts_path) == []
        assert _events(h.recorder, GameEnded)[0].end_reason == "game_over"
        # the gate verified the move itself: the slow legacy settle ran only
        # for the initial acquisition (state None), never after the swipe
        assert [s for s in settles if s is not None] == []

    def test_false_intermediate_frame_is_not_accepted(self, tmp_path: Path) -> None:
        """(б) Mid-slide frames (an empty cell transiently showing a foreign
        tile) never pass the gate: it keeps polling and accepts only the
        settled frame -- still without falling back to the legacy settle."""
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            animate_after_swipe=(TRANSIENT_A, TRANSIENT_B),
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, predict_gate_max_wait_s=60.0),
        )
        settles = _spy_in_game_settles(h)
        h.runner.run()

        assert not phone.animate_reads  # transient frames WERE seen (by the gate)...
        assert _alert_lines(h.alerts_path) == []  # ...and produced no alert
        spawns = [e for e in _events(h.recorder, TileSpawned) if e.ply >= 0]
        assert [s.spawn for s in spawns] == [Spawn(cell=0, exponent=1)]
        assert spawns[0].board_after == DEAD_BOARD  # verified on the SETTLED frame
        assert [s for s in settles if s is not None] == []

    def test_impossible_spawn_value_is_rejected_by_the_gate(self, tmp_path: Path) -> None:
        """(б') A frame that IS prediction + one new tile, but of a value the
        app never spawns (8): the 2/4 filter refuses it and the gate accepts
        the next, genuinely settled frame -- the recorded spawn is the real
        one, never the transient."""
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            animate_after_swipe=(FALSE_EARLY_FRAME,),
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, predict_gate_max_wait_s=60.0),
        )
        h.runner.run()

        assert not phone.animate_reads
        spawns = [e for e in _events(h.recorder, TileSpawned) if e.ply >= 0]
        assert [s.spawn for s in spawns] == [Spawn(cell=0, exponent=1)]  # NOT exponent 3
        assert _alert_lines(h.alerts_path) == []

    def test_gate_timeout_falls_back_to_legacy_settle(self, tmp_path: Path) -> None:
        """(в) The gate exhausts its budget on a transient frame -> the old
        stable-frame settle verifies the move exactly as before the gate."""
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            animate_after_swipe=(TRANSIENT_A,),
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(
                experiment_id="t",
                max_games=1,
                # the fake clock advances 1 s per reading: the gate sees the
                # transient frame once, times out, and must fall back
                predict_gate_max_wait_s=0.0,
                settle_max_wait_s=60.0,
            ),
        )
        settles = _spy_in_game_settles(h)
        h.runner.run()

        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 1  # the fallback settle verified the move
        spawns = [e for e in _events(h.recorder, TileSpawned) if e.ply >= 0]
        assert [s.spawn for s in spawns] == [Spawn(cell=0, exponent=1)]
        assert _alert_lines(h.alerts_path) == []
        assert len([s for s in settles if s is not None]) == 1  # legacy path ran

    def test_gate_disabled_restores_legacy_settle_only(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, predict_gate=False),
        )
        settles = _spy_in_game_settles(h)
        h.runner.run()

        assert len(_events(h.recorder, MovePlayed)) == 1
        assert len([s for s in settles if s is not None]) == 1  # pre-gate behavior

    def test_settle_delay_default_lowered_to_buffer_over_animation(self) -> None:
        assert LiveConfig(experiment_id="t").settle_delay_s == SETTLE_DELAY_DEFAULT_S
        assert pytest.approx(0.13) == SETTLE_DELAY_DEFAULT_S

    def test_settle_delay_apk_trims_the_blind_sleep(self) -> None:
        # The APK packet gate accepts the settled spawn packet whenever it
        # lands, so its blind pre-sleep only needs a fraction of the frame
        # path's animation buffer (cli.py resolves this when --settle-delay
        # is unset; an explicit flag always wins).
        assert pytest.approx(0.05) == SETTLE_DELAY_APK_S
        assert SETTLE_DELAY_APK_S < SETTLE_DELAY_DEFAULT_S

    def test_apk_lull_poll_quantum_is_fine(self) -> None:
        # Latency-scout item #2: on a packet lull the accept-only gate sleeps
        # this long between mailbox reads, so a settled spawn packet dwells up
        # to one quantum before it is consumed. poll() is a lock+read (no socket
        # I/O) and the gate cannot misclassify (exact predicted board + settled
        # 2/4 spawn), so the quantum is kept small to bound that added perceive
        # latency. Guard against a regression back to the old ~20 ms dwell.
        assert pytest.approx(0.005) == _APK_LULL_POLL_S
        assert _APK_LULL_POLL_S <= 0.01

    def test_negative_gate_budgets_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            LiveConfig(experiment_id="t", predict_gate_poll_s=-0.1)
        with pytest.raises(ValueError):
            LiveConfig(experiment_id="t", predict_gate_max_wait_s=-1.0)


# -- pause gate ----------------------------------------------------------------------


class TestPauseGate:
    def test_parks_between_moves_and_notifies(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            # start_grace_s=0: this test counts _sleep calls, so the grace
            # countdown (which also sleeps) must not shift the poll accounting.
            config=LiveConfig(
                experiment_id="t", max_games=1, pause_poll_s=2.0, start_grace_s=0.0
            ),
        )
        flag = h.runner.pause_flag
        flag.write_text("test pause\n", encoding="utf-8")
        polls = 0

        def sleeping(_s: float) -> None:
            nonlocal polls
            polls += 1
            if polls >= 3:
                flag.unlink(missing_ok=True)

        h.runner._sleep = sleeping  # inject after construction: count pause polls
        h.runner.run()

        codes = [c for _, c, _ in h.notifications]
        assert codes.index("PAUSED") < codes.index("RESUMED")
        paused_text = next(t for _, c, t in h.notifications if c == "PAUSED")
        resumed_text = next(t for _, c, t in h.notifications if c == "RESUMED")
        assert paused_text == "⏸ Пауза — можеш взяти телефон"
        assert resumed_text == "🚫 Продовжую — не чіпай телефон"
        assert polls >= 3
        # the game still completed after the pause
        assert _events(h.recorder, GameEnded)[0].end_reason == "game_over"


# -- start grace: hands-off countdown before the first capture (P14) -------------------


class TestStartGrace:
    def test_notifies_and_waits_the_configured_period(self, tmp_path: Path) -> None:
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, start_grace_s=12.0),
        )
        slept: list[float] = []
        h.runner._sleep = lambda s: slept.append(s)
        h.runner._start_grace()

        assert sum(slept) == pytest.approx(12.0)  # exactly the grace window
        grace = [n for n in h.notifications if n[1] == "START_GRACE"]
        assert len(grace) == 1  # one clear "hands off" notice
        assert "12" in grace[0][2]

    def test_zero_grace_is_a_noop(self, tmp_path: Path) -> None:
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, start_grace_s=0.0),
        )
        slept: list[float] = []
        h.runner._sleep = lambda s: slept.append(s)
        h.runner._start_grace()

        assert slept == []  # no wait
        assert [n for n in h.notifications if n[1] == "START_GRACE"] == []

    def test_grace_notice_precedes_the_first_swipe(self, tmp_path: Path) -> None:
        """The 'hands off' notice fires before the phone is ever touched."""
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        first_swipe_at: list[int] = []
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, start_grace_s=6.0),
        )

        original = h.runner.device.swipe_direction

        def spy(*args: object, **kwargs: object) -> object:
            first_swipe_at.append(len(h.notifications))
            return original(*args, **kwargs)  # type: ignore[arg-type]

        h.runner.device.swipe_direction = spy  # type: ignore[method-assign]
        h.runner.run()

        codes = [c for _, c, _ in h.notifications]
        assert "START_GRACE" in codes
        # the grace notice was emitted before the first swipe was ever sent
        assert first_swipe_at and codes.index("START_GRACE") < first_swipe_at[0]
        assert _events(h.recorder, GameEnded)[0].end_reason == "game_over"

    def test_negative_grace_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="start_grace_s"):
            LiveConfig(experiment_id="t", start_grace_s=-1.0)

    def test_default_grace_is_ten_seconds(self) -> None:
        assert LiveConfig(experiment_id="t").start_grace_s == 10.0


# -- storage trouble is WARN, the game keeps going --------------------------------------


class TestStorageError:
    def test_recorder_failure_is_warn_not_stop(self, tmp_path: Path) -> None:
        from bot2048.storage.errors import RecorderError

        class FlakyRecorder(ListRecorder):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next = 1

            def record(self, event: object) -> None:  # type: ignore[override]
                if isinstance(event, MovePlayed) and self.fail_next:
                    self.fail_next = 0
                    raise RecorderError("disk hiccup (test)")
                super().record(event)  # type: ignore[arg-type]

        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
            recorder=FlakyRecorder(),
        )
        h.runner.run()  # does not raise
        warns = [a for a in _alert_lines(h.alerts_path) if a["level"] == "WARN"]
        assert [a["code"] for a in warns] == ["STORAGE_ERROR"]
        assert _events(h.recorder, GameEnded)[0].end_reason == "game_over"


# -- mid-game abort still finalizes the summary row --------------------------------------


#: Two mergeable tiles + plenty of room: supports several verified moves.
OPEN_BOARD = (1, 1) + (0,) * 14


class TestAbortFinalizesGame:
    def test_fatal_mid_game_finalizes_row_and_propagates(self, tmp_path: Path) -> None:
        """Supervisor FATAL mid-game: GameEnded('screen_unexpected'), exception re-raised."""
        phone = FakePhone(
            board=OPEN_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            break_after_swipes=2,  # move 1 verifies, move 2 hits an UNKNOWN screen
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        with pytest.raises(FatalIncident) as exc_info:
            h.runner.run()  # the original exception still reaches the caller
        assert exc_info.value.alert.code.value == "SCREEN_UNEXPECTED"

        # the row is finalized from the last VERIFIED move, not left NULL
        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 1  # the second move never verified -> never recorded
        ended = _events(h.recorder, GameEnded)
        assert len(ended) == 1  # exactly one GameEnded: no double emit
        assert ended[0].end_reason == "screen_unexpected"  # the actual incident, not 'error'
        assert ended[0].num_moves == 1
        assert ended[0].final_score == moves[0].score_total
        assert ended[0].final_score > 0  # the 2+2 merge scored
        assert ended[0].max_exponent == 2
        assert ended[0].duration_s > 0.0
        fatal = [a for a in _alert_lines(h.alerts_path) if a["level"] == "FATAL"]
        assert len(fatal) == 1 and fatal[0]["code"] == "SCREEN_UNEXPECTED"

    def test_deliberate_stop_finalizes_as_interrupted(self, tmp_path: Path) -> None:
        """KeyboardInterrupt mid-game: GameEnded('interrupted'), then re-raised."""
        phone = FakePhone(board=OPEN_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)]))
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        def stopping(_s: float) -> None:
            if len(phone.swipes) >= 2:  # during move 2, after move 1 was committed
                raise KeyboardInterrupt

        h.runner._sleep = stopping  # inject after construction (see pause test)
        with pytest.raises(KeyboardInterrupt):
            h.runner.run()

        ended = _events(h.recorder, GameEnded)
        assert len(ended) == 1
        assert ended[0].end_reason == "interrupted"
        assert ended[0].num_moves == 1
        assert ended[0].final_score == _events(h.recorder, MovePlayed)[0].score_total


# -- --stop-before-win: hold the board one swipe from 2048 ----------------------


class TestStopBeforeWin:
    #: Two 1024 tiles (exp 10) on the top row: a RIGHT move MERGES them into a
    #: single 2048 tile (exp 11 == WIN_EXPONENT) -- the winning/forbidden move.
    ONE_SWIPE_FROM_WIN = (10, 10) + (0,) * 14

    def test_holds_and_does_not_swipe_when_enabled(self, tmp_path: Path) -> None:
        phone = FakePhone(board=self.ONE_SWIPE_FROM_WIN)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            # max_games > 1 proves the hold stops the session regardless.
            config=LiveConfig(experiment_id="t", max_games=3, stop_before_win=True),
        )
        h.runner.run()  # must NOT raise: a normal, intended stop

        # The winning swipe was WITHHELD -- the board is untouched on the phone.
        assert phone.swipes == []
        ended = _events(h.recorder, GameEnded)
        assert [e.end_reason for e in ended] == ["held_before_win"]
        assert ended[0].num_moves == 0  # no move committed
        assert _events(h.recorder, MovePlayed) == []
        # A HELD notification naming the winning direction reaches the user.
        held = [n for n in h.notifications if n[1] == "HELD"]
        assert len(held) == 1
        assert "RIGHT" in held[0][2]

    def test_swipes_winning_move_when_disabled(self, tmp_path: Path) -> None:
        phone = FakePhone(board=self.ONE_SWIPE_FROM_WIN)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            # Flag OFF (default): the winning move is played as usual; the move
            # cap ends the game right after so the scripted strategy is enough.
            config=LiveConfig(experiment_id="t", max_games=1, max_moves_per_game=1),
        )
        h.runner.run()

        # Existing behavior: the winning move IS swiped to the phone.
        assert phone.swipes == [Direction.RIGHT]
        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 1
        assert moves[0].direction is Direction.RIGHT
        ended = _events(h.recorder, GameEnded)
        assert [e.end_reason for e in ended] == ["aborted"]  # move cap, not held

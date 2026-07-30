"""Start-of-session «Нова гра» reset wiring in the live loop (#46).

Covers :attr:`LiveConfig.reset_on_start`: a successful reset seeds the first
game from the verified fresh board + zeroed timer, and every failure mode
(unsafe screen -> ``NewGameError``; retries exhausted -> ``ok`` False) is
BEST-EFFORT -- it only warns and the loop adopts the on-screen game, never
crashing the campaign. All against the in-memory fake phone, zero real delays.
"""

from __future__ import annotations

from pathlib import Path

from bot2048.device import ui_map
from bot2048.device.adb import DeviceError
from bot2048.recording import GameEnded, GameStarted, ListRecorder
from bot2048.runner.live import LiveConfig
from bot2048.vision.states import ScreenState, StateDetection
from tests.runner.conftest import (
    FRESH_BOARD,
    NEARLY_DEAD_BOARD,
    FakePhone,
    Image,
    ScriptedStrategy,
    make_harness,
)


def _events(recorder: ListRecorder, kind: type) -> list:
    return [e for e in recorder.events if isinstance(e, kind)]


def _notify_codes(notifications: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    return [(level, code) for level, code, _ in notifications]


class _UnsafeOncePhone(FakePhone):
    """detect() reports ONE unsafe (UNKNOWN) frame, then behaves normally.

    Models the screen that reset_to_new_game refuses to tap into: the reset's
    first probe sees an unsafe state and raises ``NewGameError`` before any tap,
    while every later probe (the fallback acquire, the supervisor) sees the
    real GAME screen -- so the campaign proceeds after the best-effort warning.
    """

    _unsafe_frames: int = 1

    def detect(self, image: object) -> StateDetection:
        if self._unsafe_frames > 0:
            self._unsafe_frames -= 1
            return StateDetection(ScreenState.UNKNOWN, "fake pre-game frame", 0.0)
        return super().detect(image)  # type: ignore[arg-type]


class _AuthOnNewGamePhone(FakePhone):
    """The reset's «Так, нова гра» confirm lands on the PIN pad (#46).

    Models the live failure behind the reset-on-start FATAL: the bank re-routes
    the New-Game route through re-authorization (expired session token), the
    screen goes FLAG_SECURE (the base fake's screenshot raises
    ``EmptyScreencapError`` while in ``auth``), and the fresh board only
    appears once the PIN clears the wall.
    """

    def tap(self, x: int, y: int) -> None:
        if (x, y) == ui_map.CONFIRM_YES and self.state == "confirm":
            self.taps.append((x, y))
            self.state = "auth"
            self._pending_fresh_after_auth = True
            return
        super().tap(x, y)


class _CaptureDiesOnBoardReadPhone(FakePhone):
    """screenshot() raises a device-level error on the reset's BOARD READ.

    The reset takes three auth-tolerant probes (initial, post-tap,
    post-confirm; all via ``probe_screen``) and then one raw screenshot for the
    fresh-board read -- the 4th capture. Failing exactly there models a device
    error that escapes ``reset_to_new_game`` itself and exercises the runner's
    device-level fail-safe (#46: previously a session FATAL).
    """

    _shots: int = 0

    def screenshot(self) -> Image:
        self._shots += 1
        if self._shots == 4:
            raise DeviceError("adb: screencap died mid-reset (fake)")
        return super().screenshot()


def _config(**over: object) -> LiveConfig:
    base: dict[str, object] = {
        "experiment_id": "t",
        "max_games": 1,
        "max_moves_per_game": 0,  # abort at ply 0 -> one clean 'aborted' game
        "start_grace_s": 0.0,
        "reset_on_start": True,
    }
    base.update(over)
    return LiveConfig(**base)  # type: ignore[arg-type]


class TestResetOnStart:
    def test_success_seeds_fresh_board_and_taps_new_game(self, tmp_path: Path) -> None:
        """A game in progress: reset taps «Нова гра» -> confirm, first game is
        the verified fresh board (zeroed timer), NOT the stale on-screen one."""
        phone = FakePhone(board=NEARLY_DEAD_BOARD)  # non-fresh game on screen
        h = make_harness(
            tmp_path, phone, strategy=ScriptedStrategy([]), config=_config()
        )
        h.runner.run()

        # reset tapped the calibrated NEW_GAME -> CONFIRM_YES at session start
        assert ui_map.NEW_GAME in phone.taps
        assert ui_map.CONFIRM_YES in phone.taps
        assert phone.taps.index(ui_map.NEW_GAME) < phone.taps.index(ui_map.CONFIRM_YES)

        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        # seed used: initial board is the reset's FRESH_BOARD, not NEARLY_DEAD
        assert started[0].initial_board == FRESH_BOARD
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["aborted"]

    def test_unsafe_screen_warns_and_falls_back(self, tmp_path: Path) -> None:
        """NewGameError (unsafe screen): warn, refuse to tap, adopt the current
        game exactly as with reset off -- no crash, no FATAL."""
        phone = _UnsafeOncePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path, phone, strategy=ScriptedStrategy([]), config=_config()
        )
        h.runner.run()  # must NOT raise

        # fail-closed: nothing was tapped into the bank UI on the unsafe screen
        assert ui_map.NEW_GAME not in phone.taps
        assert ui_map.CONFIRM_YES not in phone.taps
        assert ("INFO", "RESET_ON_START") in _notify_codes(h.notifications)

        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == NEARLY_DEAD_BOARD  # fell back to current
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["aborted"]

    def test_retries_exhausted_warns_and_falls_back(self, tmp_path: Path) -> None:
        """ok=False (board never verifies fresh): warn and adopt the current
        game -- the reset tapped, but the campaign still starts, never crashes."""
        # fresh_board stays non-fresh, so every reset attempt fails verification.
        phone = FakePhone(board=NEARLY_DEAD_BOARD, fresh_board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path, phone, strategy=ScriptedStrategy([]), config=_config()
        )
        h.runner.run()  # must NOT raise

        # the screen WAS safe, so the reset did tap -- it just never verified
        assert ui_map.NEW_GAME in phone.taps
        assert ui_map.CONFIRM_YES in phone.taps
        assert ("INFO", "RESET_ON_START") in _notify_codes(h.notifications)

        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == NEARLY_DEAD_BOARD  # fell back to current
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["aborted"]

    def test_auth_wall_on_reset_is_driven_through_auth_flow(self, tmp_path: Path) -> None:
        """#46 regression: the New-Game route lands on the PIN/auth screen; the
        reset drives it through the EXISTING auto-login (AuthFlow) and still
        seeds the verified fresh board -- no EmptyScreencapError FATAL."""
        phone = _AuthOnNewGamePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=_config(auto_login=True),
            auto_pin="1234",
        )
        h.runner.run()  # must NOT raise (was: EmptyScreencapError -> session FATAL)

        # the wall was cleared by the EXISTING pin-pad path (calibrated keys only)
        pin_points = set(ui_map.PIN_KEYS.values())
        assert sum(1 for t in phone.taps if t in pin_points) >= len("1234")
        assert ("INFO", "AUTH") in _notify_codes(h.notifications)

        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == FRESH_BOARD  # the reset seed survived auth
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["aborted"]

    def test_device_error_during_reset_warns_and_continues(self, tmp_path: Path) -> None:
        """A device-level error escaping the reset (capture died on the board
        read) is fail-SAFE: WARN + skip the reset and the session continues on
        the current game -- never a crash (#46)."""
        phone = _CaptureDiesOnBoardReadPhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path, phone, strategy=ScriptedStrategy([]), config=_config()
        )
        h.runner.run()  # must NOT raise

        # the failure branch (not the seed) was taken: best-effort warning sent
        assert ("INFO", "RESET_ON_START") in _notify_codes(h.notifications)
        started = _events(h.recorder, GameStarted)
        assert len(started) == 1  # session went on to play a game
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["aborted"]

    def test_disabled_does_not_reset(self, tmp_path: Path) -> None:
        """reset_on_start=False: no tap, old behavior (adopt on-screen game)."""
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=_config(reset_on_start=False),
        )
        h.runner.run()

        assert phone.taps == []  # reset never ran
        assert ("INFO", "RESET_ON_START") not in _notify_codes(h.notifications)
        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == NEARLY_DEAD_BOARD

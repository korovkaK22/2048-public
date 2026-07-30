"""Start-of-session RESUME of an in-progress game (P5, ``resume_on_start``).

Covers :attr:`LiveConfig.resume_on_start`: on a (crash-)restart the runner
adopts the non-fresh in-progress board already on screen instead of tapping
«Нова гра» and wiping it -- and falls through to the UNCHANGED
``reset_on_start`` flow in every other case (fresh board, non-GAME screen,
flag off). Precedence under test: resume overrides reset ONLY when a
resumable in-progress game is detected. All against the in-memory fake
phone, zero real delays.
"""

from __future__ import annotations

from pathlib import Path

from bot2048.core.types import Direction
from bot2048.device import ui_map
from bot2048.recording import GameEnded, GameStarted, ListRecorder, MovePlayed
from bot2048.runner.live import LiveConfig
from bot2048.vision.states import ScreenState, StateDetection
from tests.runner.conftest import (
    FRESH_BOARD,
    NEARLY_DEAD_BOARD,
    FakePhone,
    ScriptedStrategy,
    make_harness,
)


def _events(recorder: ListRecorder, kind: type) -> list:
    return [e for e in recorder.events if isinstance(e, kind)]


def _notify_codes(notifications: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    return [(level, code) for level, code, _ in notifications]


class _UnsafeFramesPhone(FakePhone):
    """detect() reports N UNKNOWN frames, then behaves normally.

    Frame 1 is the resume probe (declines: not a GAME screen), frame 2 is the
    reset's first probe (``NewGameError``, fail-closed, zero taps) -- every
    later probe (the fallback acquire) sees the real GAME screen, so the
    campaign proceeds exactly as with resume off.
    """

    _unsafe_frames: int = 2

    def detect(self, image: object) -> StateDetection:
        if self._unsafe_frames > 0:
            self._unsafe_frames -= 1
            return StateDetection(ScreenState.UNKNOWN, "fake pre-game frame", 0.0)
        return super().detect(image)  # type: ignore[arg-type]


def _config(**over: object) -> LiveConfig:
    base: dict[str, object] = {
        "experiment_id": "t",
        "max_games": 1,
        "max_moves_per_game": 0,  # abort at ply 0 -> one clean 'aborted' game
        "start_grace_s": 0.0,
        "reset_on_start": True,  # the (default-ON) wiping flow resume must beat
        "resume_on_start": True,
    }
    base.update(over)
    return LiveConfig(**base)  # type: ignore[arg-type]


class TestResumeOnStart:
    def test_in_progress_board_resumed_without_new_game_tap(
        self, tmp_path: Path
    ) -> None:
        """(a) A non-fresh in-progress board + resume_on_start: NO «Нова гра»
        tap ever happens and play continues FROM that board (the move is
        played on the resumed position, not on a wiped fresh one)."""
        phone = FakePhone(board=NEARLY_DEAD_BOARD)  # in-progress, non-fresh
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=_config(max_moves_per_game=None),
        )
        h.runner.run()

        # resume overrode reset: the board was never wiped
        assert ui_map.NEW_GAME not in phone.taps
        assert ui_map.CONFIRM_YES not in phone.taps
        assert ("INFO", "RESUME_ON_START") in _notify_codes(h.notifications)

        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == NEARLY_DEAD_BOARD  # adopted, not wiped
        # play continued from the resumed board: RIGHT was swiped and verified,
        # and the resulting dead board ended the game as a NATURAL game over.
        assert phone.swipes == [Direction.RIGHT]
        assert len(_events(h.recorder, MovePlayed)) == 1
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["game_over"]

    def test_fresh_board_falls_through_to_reset(self, tmp_path: Path) -> None:
        """(b) A FRESH board on screen + resume_on_start: nothing to lose, so
        the normal reset/new-game flow runs unchanged (taps and all)."""
        phone = FakePhone(board=FRESH_BOARD)  # brand-new game on screen
        h = make_harness(
            tmp_path, phone, strategy=ScriptedStrategy([]), config=_config()
        )
        h.runner.run()

        # the unchanged reset flow tapped NEW_GAME -> CONFIRM_YES as today
        assert ui_map.NEW_GAME in phone.taps
        assert ui_map.CONFIRM_YES in phone.taps
        assert ("INFO", "RESUME_ON_START") not in _notify_codes(h.notifications)

        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == FRESH_BOARD  # the reset's seed
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["aborted"]

    def test_non_game_screen_falls_through_unchanged(self, tmp_path: Path) -> None:
        """(c) No game / not on the game screen: resume declines (zero taps)
        and the existing reset_on_start behavior runs exactly as today
        (here: unsafe screen -> best-effort WARN -> adopt the current game)."""
        phone = _UnsafeFramesPhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path, phone, strategy=ScriptedStrategy([]), config=_config()
        )
        h.runner.run()  # must NOT raise

        # neither resume nor reset tapped into the bank UI on the unsafe screen
        assert ui_map.NEW_GAME not in phone.taps
        assert ui_map.CONFIRM_YES not in phone.taps
        assert ("INFO", "RESUME_ON_START") not in _notify_codes(h.notifications)
        # the UNCHANGED reset fail-safe fired (its best-effort warning)
        assert ("INFO", "RESET_ON_START") in _notify_codes(h.notifications)

        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == NEARLY_DEAD_BOARD  # fallback adopt
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["aborted"]

    def test_reset_default_unchanged_when_resume_off(self, tmp_path: Path) -> None:
        """(d) resume_on_start=False (the default): reset_on_start behaves
        byte-identically to today -- «Нова гра» is tapped and the first game
        starts from the verified fresh board."""
        assert LiveConfig().resume_on_start is False  # default OFF
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=_config(resume_on_start=False),
        )
        h.runner.run()

        assert ui_map.NEW_GAME in phone.taps
        assert ui_map.CONFIRM_YES in phone.taps
        assert phone.taps.index(ui_map.NEW_GAME) < phone.taps.index(ui_map.CONFIRM_YES)
        assert ("INFO", "RESUME_ON_START") not in _notify_codes(h.notifications)

        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == FRESH_BOARD  # wiped, as today
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["aborted"]

    def test_auth_wall_cleared_then_resume(self, tmp_path: Path) -> None:
        """(P5 spec 5) AUTH_REQUIRED at start: the EXISTING opt-in auto-login
        runs FIRST, THEN resume detection adopts the in-progress board behind
        the wall -- «Нова гра» is never tapped."""
        phone = FakePhone(board=NEARLY_DEAD_BOARD, state="auth")
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=_config(auto_login=True),
            auto_pin="1234",
        )
        h.runner.run()  # must NOT raise

        # the wall was cleared by the EXISTING pin-pad path (calibrated keys only)
        pin_points = set(ui_map.PIN_KEYS.values())
        assert sum(1 for t in phone.taps if t in pin_points) >= len("1234")
        # ... and the game behind it was RESUMED, not wiped
        assert ui_map.NEW_GAME not in phone.taps
        assert ui_map.CONFIRM_YES not in phone.taps
        assert ("INFO", "RESUME_ON_START") in _notify_codes(h.notifications)

        started = _events(h.recorder, GameStarted)
        assert len(started) == 1
        assert started[0].initial_board == NEARLY_DEAD_BOARD
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["aborted"]

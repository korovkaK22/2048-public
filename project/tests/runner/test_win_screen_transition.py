"""Post-2048 transition: the FLAG_SECURE «Ви зібрали 2048!» screen right after
a banked win must NEVER be treated as AUTH_REQUIRED / auto-login / device_lost.

The win screen is FLAG_SECURE (docs/device-ui-notes.md): ``screencap`` returns
0 bytes and ``uiautomator dump`` yields no hierarchy, so the restart probe
degrades to a blind hold state (SECURE_UNCONFIRMED / image-less UNKNOWN).
LIVE incident 20260711T210529Z showed the generic policy FATALing that state
right after ``_finish_win``. The fix: :meth:`Supervisor.start_new_game`
accepts ``after_win=True`` from callers that JUST positively identified the
win popup (WIN_POPUP_2048 APK anchors) and treats the blind probe as the
expected win terminal -- one calibrated tap on ``ui_map.WIN_NEW_GAME``.

Doctrine locks (must hold forever):

* zero-blind-tap: WITHOUT ``after_win`` the same blind probe still FATALs
  with zero taps;
* a REAL auth wall (AUTH_REQUIRED anchor in the dump) is never excused by
  ``after_win`` -- no auto-login, no PIN taps from the restart flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from bot2048.core.types import Direction
from bot2048.device import ui_map
from bot2048.device.adb import EmptyScreencapError
from bot2048.device.supervisor import FatalIncident, Supervisor, SupervisorConfig
from bot2048.runner.alerts import AlertWriter
from bot2048.runner.live import LiveConfig
from tests.runner.conftest import (
    FRESH_BOARD,
    NEARLY_DEAD_BOARD,
    FakePhone,
    ScriptedStrategy,
    make_harness,
)

PIN_POINTS = frozenset(ui_map.PIN_KEYS.values())


@dataclass
class SecureWinPhone(FakePhone):
    """FakePhone with a FLAG_SECURE-blind win screen (state ``securewin``).

    On the real device the win popup is only visible on the APK stream; to
    ``screencap`` it is 0 bytes and to ``uiautomator dump`` a blank hierarchy,
    so ``probe_screen`` degrades to SECURE_UNCONFIRMED. Tapping the calibrated
    «Нова гра» point starts a fresh game (optionally after ``blind_holds``
    more blind supervisor sleeps -- the win -> board transition can stay
    blind for a beat, LIVE 20260711T210529Z).
    """

    blind_holds: int = 0  # supervisor sleeps to stay blind for after the tap

    def screenshot(self):  # type: ignore[no-untyped-def]  # matches FakePhone
        if self.state == "securewin":
            raise EmptyScreencapError("screencap 0 bytes (FLAG_SECURE win screen)")
        return super().screenshot()

    def ui_dump(self) -> str:
        if self.state == "securewin":
            return ""  # blank hierarchy: no anchors at all (not an auth wall)
        return super().ui_dump()

    def tap(self, x: int, y: int) -> None:
        if self.state == "securewin":
            self.taps.append((x, y))
            if (x, y) == ui_map.WIN_NEW_GAME and self.blind_holds == 0:
                self.state = "game"
                self.board = self.fresh_board
            # else: stay blind; on_sleep() below resolves the hold
            return
        super().tap(x, y)

    def on_sleep(self) -> None:
        """Supervisor sleep hook: resolve the blind hold one beat at a time."""
        if self.state == "securewin" and ui_map.WIN_NEW_GAME in self.taps:
            if self.blind_holds > 0:
                self.blind_holds -= 1
            if self.blind_holds == 0:
                self.state = "game"
                self.board = self.fresh_board


def _make_supervisor(tmp_path: Path, phone: FakePhone) -> Supervisor:
    return Supervisor(
        phone,
        phone.detect,
        AlertWriter(tmp_path / "alerts.jsonl"),
        SupervisorConfig(incidents_root=tmp_path / "incidents"),
        sleep=lambda _s: getattr(phone, "on_sleep", lambda: None)(),
    )


class TestWinBlindRestart:
    def test_after_win_blind_screen_taps_win_new_game(self, tmp_path: Path) -> None:
        """after_win=True: the blind probe IS the win screen -> one calibrated tap."""
        phone = SecureWinPhone(board=NEARLY_DEAD_BOARD, state="securewin")
        sup = _make_supervisor(tmp_path, phone)

        t0, frame = sup.start_new_game(after_win=True)

        assert phone.taps == [ui_map.WIN_NEW_GAME]
        assert not PIN_POINTS.intersection(phone.taps)  # no auto-login ever
        assert phone.board == FRESH_BOARD
        assert frame is not None and t0 > 0

    def test_blind_screen_without_after_win_fatals_zero_taps(self, tmp_path: Path) -> None:
        """Zero-blind-tap doctrine: no after_win -> FATAL, not a single tap."""
        phone = SecureWinPhone(board=NEARLY_DEAD_BOARD, state="securewin")
        sup = _make_supervisor(tmp_path, phone)

        with pytest.raises(FatalIncident):
            sup.start_new_game()
        assert phone.taps == []

    def test_real_auth_wall_never_excused_by_after_win(self, tmp_path: Path) -> None:
        """AUTH_REQUIRED (anchor in the dump) is NOT a blind hold: FATAL, no taps."""
        phone = FakePhone(board=NEARLY_DEAD_BOARD, state="auth")
        sup = _make_supervisor(tmp_path, phone)  # no auth resolver wired

        with pytest.raises(FatalIncident):
            sup.start_new_game(after_win=True)
        assert ui_map.WIN_NEW_GAME not in phone.taps
        assert not PIN_POINTS.intersection(phone.taps)

    def test_blind_transition_after_tap_repolls_until_fresh_board(
        self, tmp_path: Path
    ) -> None:
        """The win -> board transition staying blind for a beat is re-polled
        tap-lessly (never a second tap, never AUTH/auto-login)."""
        phone = SecureWinPhone(
            board=NEARLY_DEAD_BOARD, state="securewin", blind_holds=2
        )
        sup = _make_supervisor(tmp_path, phone)

        _t0, _frame = sup.start_new_game(after_win=True)

        assert phone.taps == [ui_map.WIN_NEW_GAME]  # exactly ONE tap
        assert phone.board == FRESH_BOARD


class TestLiveRunnerWiring:
    """The runner passes after_win=True exactly when a 2048 win just banked."""

    @staticmethod
    def _spy(harness_runner: object) -> list[bool]:
        sup = harness_runner.supervisor  # type: ignore[attr-defined]
        calls: list[bool] = []
        orig = sup.start_new_game

        def wrapped(*, after_win: bool = False):  # type: ignore[no-untyped-def]
            calls.append(after_win)
            return orig(after_win=after_win)

        sup.start_new_game = wrapped  # type: ignore[method-assign]
        return calls

    def test_win_restart_passes_after_win_true(self, tmp_path: Path) -> None:
        phone = FakePhone(board=NEARLY_DEAD_BOARD, win_after_swipes=1)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.LEFT]),
            config=LiveConfig(experiment_id="t", max_games=2, max_moves_per_game=1),
        )
        calls = self._spy(h.runner)
        h.runner.run()
        assert calls == [True]
        assert not PIN_POINTS.intersection(phone.taps)

    def test_game_over_restart_passes_after_win_false(self, tmp_path: Path) -> None:
        phone = FakePhone(board=NEARLY_DEAD_BOARD, game_over_after_swipes=1)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.LEFT]),
            config=LiveConfig(experiment_id="t", max_games=2, max_moves_per_game=1),
        )
        calls = self._spy(h.runner)
        h.runner.run()
        assert calls == [False]

    def test_stale_win_popup_at_start_passes_after_win_true(self, tmp_path: Path) -> None:
        phone = FakePhone(board=NEARLY_DEAD_BOARD, state="win")
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, max_moves_per_game=1),
        )
        calls = self._spy(h.runner)
        h.runner.run()
        assert calls == [True]

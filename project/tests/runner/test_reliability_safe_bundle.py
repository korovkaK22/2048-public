"""SAFE reliability bundle: four low-risk live-runner hardening fixes.

Each test drives the REAL loop / component against the in-memory fake phone
(see :mod:`tests.runner.conftest`) with the deterministic fake clock -- zero
real delays. The four fixes:

1. legacy settle-path spawn-exponent gate (observation.py) -- a stably-misread
   non-2/4 spawn is REJECTED instead of committed;
2. move-level ``choose()`` retry (swipe_controller.py) -- one ``GoEngineError``
   restarts the engine and retries; only a SECOND failure aborts the game;
3. main-loop auth PIN retry loop (auth_flow.py) -- PIN entry honors
   ``auth_retries`` (a flaky PIN recovers; the cap is never exceeded);
4. low-score churn escalation (live.py) -- K consecutive very-short games raise
   an ERROR alert; a normal-length game resets the streak.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bot2048.ai.go_engine import GoEngineError
from bot2048.ai.strategies import Decision
from bot2048.core.types import Board, Direction, Spawn
from bot2048.device import ui_map
from bot2048.device.supervisor import FatalIncident
from bot2048.recording import GameEnded, MovePlayed, TileSpawned
from bot2048.runner.live import LiveConfig
from tests.runner.conftest import (
    NEARLY_DEAD_BOARD,
    FakePhone,
    ScriptedStrategy,
    make_harness,
)


def _alert_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _error_alerts(path: Path, code: str) -> list[dict[str, Any]]:
    return [a for a in _alert_lines(path) if a["level"] == "ERROR" and a["code"] == code]


def _events(recorder: Any, kind: type) -> list[Any]:
    return [e for e in recorder.events if isinstance(e, kind)]


# -- Fix 1: legacy settle-path spawn-exponent gate --------------------------------


class TestLegacySettleSpawnGate:
    """observation.py: the legacy stable-frame accept path must reject a
    non-2/4 spawn, exactly like the predict-gate / APK-gate paths."""

    def _run(self, tmp_path: Path, spawn_exp: int) -> Any:
        # predict_gate OFF + no board_source => verification goes through the
        # LEGACY stable-frame settle path (_observe_after_swipe_frames), the one
        # this fix gates.
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=spawn_exp)]),
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 3),
            config=LiveConfig(experiment_id="t", max_games=1, predict_gate=False),
        )
        h.runner.run()
        return h

    def test_legal_2_spawn_is_committed(self, tmp_path: Path) -> None:
        # exponent 1 == a "2" tile: a legal spawn, accepted and committed.
        h = self._run(tmp_path, spawn_exp=1)
        assert len(_events(h.recorder, MovePlayed)) >= 1
        # every committed spawn is a legal 2/4
        assert all(e.spawn.exponent in (1, 2) for e in _events(h.recorder, TileSpawned))

    def test_non_2_4_spawn_is_rejected(self, tmp_path: Path) -> None:
        # exponent 5 == a "32" tile the app can NEVER spawn: a stable misread.
        # The gate must reject it -> the move aborts vision_mismatch instead of
        # committing a corrupt board to the fixed policy.
        h = self._run(tmp_path, spawn_exp=5)
        ended = _events(h.recorder, GameEnded)
        assert [e.end_reason for e in ended] == ["vision_mismatch"]
        # no move committed, so no move-spawn (ply >= 0) was recorded -- only the
        # game's initial tiles (ply < 0) exist, and none is the corrupt exp-5
        assert _events(h.recorder, MovePlayed) == []
        move_spawns = [e for e in _events(h.recorder, TileSpawned) if e.ply >= 0]
        assert move_spawns == []
        assert all(e.spawn.exponent != 5 for e in _events(h.recorder, TileSpawned))


# -- Fix 2: move-level choose() retry ---------------------------------------------


class _FlakyChooseStrategy:
    """Raises ``GoEngineError`` on the given 1-based ``choose()`` call indices,
    otherwise plays the next scripted direction. Counts ``close()`` calls."""

    def __init__(self, directions: list[Direction], fail_on: set[int]) -> None:
        self._dirs = deque(directions)
        self._fail_on = set(fail_on)
        self.calls = 0
        self.closed = 0

    @property
    def name(self) -> str:
        return "flaky"

    def choose(self, board: Board) -> Decision:
        self.calls += 1
        if self.calls in self._fail_on:
            raise GoEngineError(f"engine hiccup (fake), call {self.calls}")
        return Decision(direction=self._dirs.popleft(), eval_ms=0.5)

    def close(self) -> None:
        self.closed += 1


class TestChooseRetry:
    def test_single_engine_error_recovers(self, tmp_path: Path) -> None:
        # First choose() raises, the retry (after close()) succeeds -> the game
        # plays on and is NOT finalized as 'error'.
        strat = _FlakyChooseStrategy([Direction.RIGHT, Direction.LEFT], fail_on={1})
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=strat,
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        h.runner.run()

        assert strat.closed == 1  # engine restarted exactly once before the retry
        assert len(_events(h.recorder, MovePlayed)) == 1  # the move committed
        ended = _events(h.recorder, GameEnded)
        assert [e.end_reason for e in ended] == ["game_over"]  # DEAD_BOARD, no 'error'

    def test_second_engine_error_aborts_as_error(self, tmp_path: Path) -> None:
        # Both the initial choose() AND the retry raise -> the game finalizes as
        # 'error' and the exception propagates (recovery disabled so it stops).
        strat = _FlakyChooseStrategy([Direction.RIGHT], fail_on={1, 2})
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=strat,
            config=LiveConfig(experiment_id="t", max_games=1, recover_max_per_hour=0),
        )
        with pytest.raises(GoEngineError):
            h.runner.run()

        assert strat.closed == 1  # one restart between the two failures
        assert strat.calls == 2  # exactly initial + one retry, then it aborts
        ended = _events(h.recorder, GameEnded)
        assert [e.end_reason for e in ended] == ["error"]
        assert _events(h.recorder, MovePlayed) == []


# -- Fix 3: main-loop auth PIN retry loop -----------------------------------------


class TestAuthPinRetry:
    def test_flaky_pin_recovers_within_the_cap(self, tmp_path: Path) -> None:
        # The auth wall clears only on the 2nd full PIN entry; with auth_retries=3
        # the bounded loop retries and the run recovers -- no FATAL.
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            auth_after_swipes=1,  # the first move's verify hits the wall
            pin_entries_to_clear=2,  # first PIN entry flaky, second clears it
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(
                experiment_id="t", max_games=1, auto_login=True, auth_retries=3
            ),
            auto_pin="1234",
        )
        h.runner.run()  # must NOT raise FatalIncident

        pin_points = set(ui_map.PIN_KEYS.values())
        # exactly two full PIN entries (2 x 4 taps): the retry recovered and the
        # loop stopped entering the PIN the moment the wall cleared
        assert len([t for t in phone.taps if t in pin_points]) == 8
        assert [a for a in _alert_lines(h.alerts_path) if a["level"] == "FATAL"] == []

    def test_persistent_wall_fatals_at_the_cap(self, tmp_path: Path) -> None:
        # The wall never clears within the cap: after auth_retries=2 entries the
        # SAME AUTH_REQUIRED FATAL fires -- and the PIN is entered no more than
        # the configured cap (lockout-safe).
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            auth_after_swipes=1,
            pin_entries_to_clear=5,  # more than the cap -> never clears
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(
                experiment_id="t", max_games=1, auto_login=True, auth_retries=2
            ),
            auto_pin="1234",
        )
        with pytest.raises(FatalIncident) as exc_info:
            h.runner.run()
        assert exc_info.value.alert.code.value == "AUTH_REQUIRED"

        pin_points = set(ui_map.PIN_KEYS.values())
        # NEVER more than auth_retries (2) full entries = 8 taps
        assert len([t for t in phone.taps if t in pin_points]) == 8


# -- Fix 4: low-score churn escalation --------------------------------------------


def _mk_runner(tmp_path: Path, **cfg: Any) -> Any:
    phone = FakePhone(board=NEARLY_DEAD_BOARD)
    return make_harness(
        tmp_path,
        phone,
        strategy=ScriptedStrategy([]),
        config=LiveConfig(experiment_id="t", **cfg),
    )


class TestLowScoreChurn:
    def test_k_low_ply_games_raise_error_alert(self, tmp_path: Path) -> None:
        h = _mk_runner(tmp_path, low_score_churn_min_ply=20, low_score_churn_limit=3)
        low = SimpleNamespace(ply=1, game_id="g")

        h.runner._note_game_ended(low, "game_over")  # streak 1
        h.runner._note_game_ended(low, "move_rejected")  # streak 2
        assert _error_alerts(h.alerts_path, "RESTART_LOOP") == []  # not yet

        h.runner._note_game_ended(low, "vision_mismatch")  # streak 3 -> alert
        alerts = _error_alerts(h.alerts_path, "RESTART_LOOP")
        assert len(alerts) == 1
        assert alerts[0]["extra"]["streak"] == 3
        # surfaced to the operator too (ERROR notification)
        assert any(
            lvl == "ERROR" and code == "RESTART_LOOP"
            for lvl, code, _ in h.notifications
        )

    def test_normal_game_resets_the_streak(self, tmp_path: Path) -> None:
        h = _mk_runner(tmp_path, low_score_churn_min_ply=20, low_score_churn_limit=3)
        low = SimpleNamespace(ply=1, game_id="g")
        normal = SimpleNamespace(ply=500, game_id="g")

        h.runner._note_game_ended(low, "game_over")  # 1
        h.runner._note_game_ended(low, "game_over")  # 2
        h.runner._note_game_ended(normal, "game_over")  # healthy -> reset
        assert h.runner._low_score_churn_streak == 0

        h.runner._note_game_ended(low, "game_over")  # 1 again
        h.runner._note_game_ended(low, "game_over")  # 2 -- never reaches 3 in a row
        assert _error_alerts(h.alerts_path, "RESTART_LOOP") == []

    def test_limit_zero_disables_the_guard(self, tmp_path: Path) -> None:
        h = _mk_runner(tmp_path, low_score_churn_limit=0)
        st = SimpleNamespace(ply=0, game_id="g")
        for _ in range(50):
            h.runner._note_game_ended(st, "game_over")
        assert _error_alerts(h.alerts_path, "RESTART_LOOP") == []
        assert h.runner._low_score_churn_streak == 0

    def test_churn_loop_fires_in_the_live_loop(self, tmp_path: Path) -> None:
        # End-to-end: each restart re-seeds a nearly-dead board, so every game
        # ends after one move (ply 1 < 20). Three such games in a row trip the
        # ERROR alert -- proving clean game-overs are now surfaced.
        phone = FakePhone(board=NEARLY_DEAD_BOARD, fresh_board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 10),
            config=LiveConfig(
                experiment_id="t",
                max_games=3,
                low_score_churn_min_ply=20,
                low_score_churn_limit=3,
            ),
        )
        h.runner.run()

        assert len(_error_alerts(h.alerts_path, "RESTART_LOOP")) == 1
        # every game really was a one-move churn game
        ended = _events(h.recorder, GameEnded)
        assert len(ended) == 3
        assert all(e.end_reason == "game_over" and e.num_moves == 1 for e in ended)

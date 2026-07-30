"""Overnight-hardening tests: auto-learn, game loop, transient recovery."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest

from bot2048.core.types import Board, Direction, Spawn
from bot2048.device import ui_map
from bot2048.recording import GameEnded, GameStarted, ListRecorder, MovePlayed, TileSpawned
from bot2048.runner.live import LiveConfig
from tests.runner.conftest import (
    NEARLY_DEAD_BOARD,
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


# -- auto-learn: engine prediction teaches vision a freshly merged tile --------------

#: RIGHT merges the 3,3 pair into a NEW exponent-4 tile at cell 1; the rest is
#: a dead checkerboard, so after the spawn at cell 0 the game is over.
LEARN_BOARD: Board = (3, 3, 5, 6, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)
PREDICTED_AFTER_LEARN: Board = (0, 4, 5, 6, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)
SETTLED_AFTER_LEARN: Board = (1, 4, 5, 6, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)


def _learn_phone(*, suspect: bool, accepts: bool = True) -> FakePhone:
    """The merged tile (cell 1, exp 4) is unknown to vision: reported as 3."""
    return FakePhone(
        board=LEARN_BOARD,
        spawn_script=deque([Spawn(cell=0, exponent=1)]),
        misread={1: 3},
        suspect_cells={1} if suspect else set(),
        learn_accepts=accepts,
    )


class TestAutoLearn:
    def test_new_merged_tile_is_learned_and_move_committed(self, tmp_path: Path) -> None:
        phone = _learn_phone(suspect=True)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, parse_retries=2),
            read_cells=phone.read_cells,
            learn_tile=phone.learn_tile,
        )
        h.runner.run()

        # learn_tile called exactly once, with the ENGINE-predicted exponent
        assert phone.learn_calls == [(1, 4)]
        assert phone.misread == {}  # in-memory calibration updated by the hook
        # the move committed with the predicted board, not aborted
        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 1
        assert moves[0].board_after == PREDICTED_AFTER_LEARN
        spawns = [e for e in _events(h.recorder, TileSpawned) if e.ply >= 0]
        assert [s.spawn for s in spawns] == [Spawn(cell=0, exponent=1)]
        assert spawns[0].board_after == SETTLED_AFTER_LEARN
        assert _events(h.recorder, GameEnded)[0].end_reason == "game_over"
        # no VISION_MISMATCH escalation happened
        assert [a["code"] for a in _alert_lines(h.alerts_path)] == []
        # the human was told about the learned tile (value 2**4 = 16, cell 1)
        learned = [n for n in h.notifications if n[1] == "TILE_LEARNED"]
        assert len(learned) == 1
        assert learned[0][0] == "INFO"
        assert "16" in learned[0][2]

    def test_fully_unknown_tile_reads_zero_and_is_learned(self, tmp_path: Path) -> None:
        """Lenient vision reports the merged tile as 0 (unknown): still learned.

        Regression for the run3 VISION_MISMATCH abort on the pink 1024: a
        fully unknown cell now parses as exponent 0 + suspect instead of
        raising, and the r=0 diff on the merge target is auto-learned from
        the engine prediction.
        """
        phone = _learn_phone(suspect=True)
        phone.misread = {}
        # value-16 tiles are unknown to vision: lenient parse reports exponent 0.
        # Pre-move reads stay clean (the exp-3 pair is a KNOWN color) -- exactly
        # the run3 incident shape (512+512 -> never-seen pink 1024 on cell 13).
        phone.misread_values = {4: 0}
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, parse_retries=2),
            read_cells=phone.read_cells,
            learn_tile=phone.learn_tile,
        )
        h.runner.run()

        assert phone.learn_calls == [(1, 4)]  # ENGINE-predicted exponent, not 0
        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 1
        assert moves[0].board_after == PREDICTED_AFTER_LEARN
        assert _events(h.recorder, GameEnded)[0].end_reason == "game_over"
        assert [a["code"] for a in _alert_lines(h.alerts_path)] == []
        assert [n[1] for n in h.notifications if n[1] == "TILE_LEARNED"] == [
            "TILE_LEARNED"
        ]

    def test_genuine_misread_still_aborts(self, tmp_path: Path) -> None:
        """Same diff but vision is CONFIDENT (clean digit read): never learn."""
        phone = _learn_phone(suspect=False)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=1, parse_retries=2),
            read_cells=phone.read_cells,
            learn_tile=phone.learn_tile,
        )
        h.runner.run()

        assert phone.learn_calls == []  # never even sampled
        errors = [a for a in _alert_lines(h.alerts_path) if a["level"] == "ERROR"]
        assert [a["code"] for a in errors] == ["VISION_MISMATCH"]
        assert _events(h.recorder, GameEnded)[0].end_reason == "vision_mismatch"
        assert _events(h.recorder, MovePlayed) == []
        assert [n for n in h.notifications if n[1] == "TILE_LEARNED"] == []

    def test_refused_sample_keeps_the_abort(self, tmp_path: Path) -> None:
        """vision.learn_tile_color says no (unsettled/ambiguous color): abort."""
        phone = _learn_phone(suspect=True, accepts=False)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=1, parse_retries=2),
            read_cells=phone.read_cells,
            learn_tile=phone.learn_tile,
        )
        h.runner.run()

        assert phone.learn_calls == [(1, 4)]  # sampled once, refused
        errors = [a for a in _alert_lines(h.alerts_path) if a["level"] == "ERROR"]
        assert [a["code"] for a in errors] == ["VISION_MISMATCH"]
        assert _events(h.recorder, MovePlayed) == []
        assert [n for n in h.notifications if n[1] == "TILE_LEARNED"] == []


# -- game loop: --games 0 plays forever, restarting after every game-over ------------


class _InterruptAfter(ScriptedStrategy):
    """RIGHT forever; raises KeyboardInterrupt on choose() call N (1-based)."""

    def __init__(self, n: int) -> None:
        super().__init__([])
        self._n = n
        self.calls = 0

    def choose(self, board):  # type: ignore[override]
        self.calls += 1
        if self.calls >= self._n:
            raise KeyboardInterrupt
        from bot2048.ai.strategies import Decision

        return Decision(direction=Direction.RIGHT, eval_ms=0.5)


class TestGameLoop:
    def test_games_zero_loops_until_stopped(self, tmp_path: Path) -> None:
        """max_games=0: three full games back-to-back, each restarted via the
        calibrated NEW_GAME -> CONFIRM_YES taps and recorded separately."""
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            fresh_board=NEARLY_DEAD_BOARD,  # every new game is one move from over
        )
        strategy = _InterruptAfter(4)  # stop during game 4
        h = make_harness(
            tmp_path,
            phone,
            strategy=strategy,
            config=LiveConfig(experiment_id="t", max_games=0),
        )
        with pytest.raises(KeyboardInterrupt):
            h.runner.run()

        started = _events(h.recorder, GameStarted)
        assert len(started) == 4
        assert len({s.game_id for s in started}) == 4  # recorded separately
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["game_over", "game_over", "game_over", "interrupted"]
        assert phone.taps.count(ui_map.NEW_GAME) == 3
        assert phone.taps.count(ui_map.CONFIRM_YES) == 3

    def test_max_games_zero_is_valid_config(self) -> None:
        assert LiveConfig(experiment_id="t", max_games=0).max_games == 0
        with pytest.raises(ValueError):
            LiveConfig(experiment_id="t", max_games=-1)


# -- transient error recovery: a device hiccup never ends the session ----------------


class TestTransientRecovery:
    def test_transient_device_error_recovers_and_continues(self, tmp_path: Path) -> None:
        reconnects: list[int] = []
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            crash_after_swipes=1,  # ONE screenshot blows up right after move 1
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=2),
            reconnect=lambda: reconnects.append(1),
        )
        h.runner.run()  # does NOT raise: the session outlives the hiccup

        # game 1 was finalized as error, then the session recovered and the
        # re-acquired (dead) board ended game 2 cleanly.
        reasons = [e.end_reason for e in _events(h.recorder, GameEnded)]
        assert reasons == ["error", "game_over"]
        assert len(reconnects) == 1  # adb reconnect attempted during recovery
        codes = {a["code"] for a in _alert_lines(h.alerts_path)}
        assert codes & {"LOOP_ERROR", "DEVICE_LOST"}  # recovery alert written
        assert (tmp_path / "incidents").exists()  # bundle for the morning triage
        assert phone.transient_errors == deque()  # the injected error WAS hit

    def test_recovery_disabled_propagates(self, tmp_path: Path) -> None:
        """recover_max_per_hour=0 restores pre-hardening fail-fast semantics."""
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            crash_after_swipes=1,
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=2, recover_max_per_hour=0),
        )
        with pytest.raises(RuntimeError):
            h.runner.run()
        # the open game was still finalized before propagating
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["error"]

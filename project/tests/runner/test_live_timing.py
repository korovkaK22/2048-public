"""Per-move phase telemetry: the live loop writes ``eval_details["timing"]``.

The harness clock (``_FakeClock``) advances exactly 1.0 s per read, so every
phase segment costs exactly 1000.0 ms -- fully deterministic, no real sleeps.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest

from bot2048.ai.strategies import Decision
from bot2048.core.types import Board, Direction, Spawn
from bot2048.recording import MovePlayed
from bot2048.runner.live import LiveConfig
from tests.runner.conftest import (
    NEARLY_DEAD_BOARD,
    FakePhone,
    ScriptedStrategy,
    make_harness,
)

TIMING_KEYS = frozenset(
    {"capture_ms", "perceive_ms", "detect_ms", "swipe_ms", "settle_ms", "total_ms"}
)

#: Two mergeable tiles + plenty of room: supports several verified moves.
OPEN_BOARD: Board = (1, 1) + (0,) * 14


class DetailedStrategy(ScriptedStrategy):
    """Scripted moves whose Decision carries pre-existing engine telemetry."""

    def __init__(self, directions: list[Direction]) -> None:
        super().__init__(directions)
        self.details: dict[str, Any] = {"engine_ms": 0.2, "prefetch_hit": True}
        self._issued: list[dict[str, Any]] = []

    def choose(self, board: Board) -> Decision:
        base = super().choose(board)
        details = dict(self.details)
        self._issued.append(details)
        return Decision(
            direction=base.direction, eval_ms=base.eval_ms, details=details
        )

    @property
    def issued(self) -> list[dict[str, Any]]:
        return self._issued


def _moves(harness: Any) -> list[MovePlayed]:
    return [e for e in harness.recorder.events if isinstance(e, MovePlayed)]


def _play_one_move(tmp_path: Path, strategy: Any) -> Any:
    phone = FakePhone(
        board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
    )
    h = make_harness(
        tmp_path,
        phone,
        strategy=strategy,
        config=LiveConfig(experiment_id="t", max_games=1),
    )
    h.runner.run()
    return h


class TestMoveTiming:
    def test_timing_subdict_has_all_keys_and_total_is_the_sum(
        self, tmp_path: Path
    ) -> None:
        h = _play_one_move(tmp_path, ScriptedStrategy([Direction.RIGHT]))
        moves = _moves(h)
        assert len(moves) == 1
        timing = moves[0].eval_details["timing"]

        assert set(timing) == TIMING_KEYS
        assert all(isinstance(v, float) for v in timing.values())
        phase_sum = sum(v for k, v in timing.items() if k != "total_ms")
        assert timing["total_ms"] == pytest.approx(phase_sum)

        # every phase the happy path exercises got a non-zero bill
        for key in ("capture_ms", "perceive_ms", "detect_ms", "swipe_ms", "settle_ms"):
            assert timing[key] > 0.0, key

    def test_existing_strategy_details_survive_untouched(self, tmp_path: Path) -> None:
        strategy = DetailedStrategy([Direction.RIGHT])
        h = _play_one_move(tmp_path, strategy)
        moves = _moves(h)
        assert len(moves) == 1
        details = moves[0].eval_details

        # engine wrapper keys (P16) are read-only for us: present and unchanged
        assert details["engine_ms"] == 0.2
        assert details["prefetch_hit"] is True
        assert "timing" in details

        # the strategy's own dict was copied, never mutated in place
        assert strategy.issued[0] == {"engine_ms": 0.2, "prefetch_hit": True}

    def test_timing_resets_between_moves(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.LEFT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        h.runner.run()
        moves = _moves(h)
        assert moves, "expected at least one recorded move"
        # with the fake clock every segment costs exactly 1000 ms, so a move's
        # total is bounded by (segments of that move) * 1000 -- an accumulator
        # that failed to reset would grow monotonically past any single-move sum
        for move in moves:
            timing = move.eval_details["timing"]
            phase_sum = sum(v for k, v in timing.items() if k != "total_ms")
            assert timing["total_ms"] == pytest.approx(phase_sum)
            assert timing["total_ms"] < 100_000.0


class TestLoopGapProbes:
    """``eval_details["move"]["t_commit"]`` / ``["prev_commit_ms"]`` (design §5.1).

    Both are additive neighbours of the frozen ``timing`` bucket: the stamp
    is monotonic and strictly increasing within a game; the persist cost of
    move n rides on move n+1 and is absent on ply 0.
    """

    def test_t_commit_monotonic_and_prev_commit_shifted_by_one(
        self, tmp_path: Path
    ) -> None:
        phone = FakePhone(board=OPEN_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.LEFT, Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, max_moves_per_game=3),
        )
        h.runner.run()
        moves = _moves(h)
        assert len(moves) == 3

        stamps = [m.eval_details["move"]["t_commit"] for m in moves]
        assert all(isinstance(t, float) for t in stamps)
        assert stamps == sorted(stamps) and len(set(stamps)) == 3

        # ply 0: no previous move in THIS game -> key absent, never fabricated
        assert "prev_commit_ms" not in moves[0].eval_details["move"]
        for move in moves[1:]:
            prev = move.eval_details["move"]["prev_commit_ms"]
            assert isinstance(prev, float)
            assert 0.0 <= prev < 10_000.0

        # the frozen timing bucket is untouched by the new neighbours
        for move in moves:
            assert set(move.eval_details["timing"]) == TIMING_KEYS

    def test_prev_commit_not_carried_across_games(self, tmp_path: Path) -> None:
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
        moves = _moves(h)
        assert len(moves) == 2
        assert [m.ply for m in moves] == [0, 0]
        # the second game's first move must NOT inherit game 1's persist cost
        assert all("prev_commit_ms" not in m.eval_details["move"] for m in moves)

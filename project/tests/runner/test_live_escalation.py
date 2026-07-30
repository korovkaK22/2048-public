"""Mid-game budget escalation: the single step (§5.2) and the 3-tier LADDER.

The hook under test is :meth:`LiveRunner._maybe_escalate_budget`, exercised
directly against a real (never-spawned) ``GoExpectimaxStrategy`` in budget
mode: constructing the strategy does NOT start the go process, and the hook
only reads/writes ``budget_ms``, so no engine binary is involved.

Two layers:

* ``TestMedianEscalation`` -- the LEGACY one-shot ``escalate_*`` knobs, which
  now map onto the ladder (ply trigger -> T1, empty trigger -> T2, both at
  ``escalate_budget_ms``); the observable behavior must stay identical.
* ``TestBudgetLadder`` -- the 3-tier exponential ladder proper
  (briefs/live-pipeline-timing.md §3): base < T1 (``late_*``) < T2
  (``endgame_*``), monotonic within a game, reset at a new game.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot2048.ai.go_engine import GoExpectimaxStrategy
from bot2048.core.types import Board
from bot2048.runner.live import LiveConfig, _GameState
from tests.runner.conftest import FRESH_BOARD, FakePhone, ScriptedStrategy, make_harness

#: 2 empty cells -- "late-game, low branching" for ``escalate_when_empty_le``.
LATE_BOARD: Board = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2, 3, 0, 0)

BASE_MS = 50.0
ESCALATED_MS = 800.0

#: The brief-§3 ladder used by the tier tests: T0 base (the strategy's own
#: ``BASE_MS``) -> T1 past the champion median -> T2 in the death-zone.
LATE_AFTER_PLY = 2067
LATE_MS = 2000.0
ENDGAME_EMPTY_LE = 3
ENDGAME_MS = 9000.0


def _ladder_config() -> LiveConfig:
    return LiveConfig(
        experiment_id="t",
        late_after_ply=LATE_AFTER_PLY,
        late_budget_ms=LATE_MS,
        endgame_when_empty_le=ENDGAME_EMPTY_LE,
        endgame_budget_ms=ENDGAME_MS,
    )


def _game(board: Board, ply: int) -> _GameState:
    state = _GameState(game_id="a" * 32, board=board, t0=0.0)
    state.ply = ply
    return state


def _budget_strategy() -> GoExpectimaxStrategy:
    return GoExpectimaxStrategy(budget_ms=BASE_MS)


class TestMedianEscalation:
    def test_escalates_exactly_once_when_ply_crossed(self, tmp_path: Path) -> None:
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=LiveConfig(
                experiment_id="t",
                escalate_after_ply=2067,
                escalate_budget_ms=ESCALATED_MS,
            ),
        )
        # Below the median: nothing happens.
        state = _game(FRESH_BOARD, ply=2066)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == BASE_MS
        assert not state.budget_escalated
        # Crossing the median: escalate + remember the base budget.
        state.ply = 2067
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == ESCALATED_MS
        assert state.budget_escalated
        assert h.runner._pre_escalation_budget_ms == BASE_MS
        # One-shot per game: a second trigger does NOT re-apply.
        strat.set_budget_ms(BASE_MS)
        state.ply = 3000
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == BASE_MS

    def test_escalates_on_low_empty_cells(self, tmp_path: Path) -> None:
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=LiveConfig(
                experiment_id="t",
                escalate_when_empty_le=2,
                escalate_budget_ms=ESCALATED_MS,
            ),
        )
        # A fresh board (14 empty cells) is far from the trigger.
        h.runner._maybe_escalate_budget(_game(FRESH_BOARD, ply=5))
        assert strat.budget_ms == BASE_MS
        # A cramped late-game board (2 empty) escalates regardless of ply.
        state = _game(LATE_BOARD, ply=5)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == ESCALATED_MS
        assert state.budget_escalated

    def test_off_by_default_all_knobs_none(self, tmp_path: Path) -> None:
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=LiveConfig(experiment_id="t"),
        )
        state = _game(LATE_BOARD, ply=999_999)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == BASE_MS
        assert not state.budget_escalated

    def test_budget_without_trigger_knob_is_noop(self, tmp_path: Path) -> None:
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=LiveConfig(experiment_id="t", escalate_budget_ms=ESCALATED_MS),
        )
        h.runner._maybe_escalate_budget(_game(LATE_BOARD, ply=999_999))
        assert strat.budget_ms == BASE_MS

    def test_skips_non_go_strategy(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=ScriptedStrategy([]),
            config=LiveConfig(
                experiment_id="t",
                escalate_after_ply=1,
                escalate_budget_ms=ESCALATED_MS,
            ),
        )
        state = _game(LATE_BOARD, ply=10)
        h.runner._maybe_escalate_budget(state)  # must not raise
        assert not state.budget_escalated

    def test_skips_fixed_depth_go_strategy(self, tmp_path: Path) -> None:
        strat = GoExpectimaxStrategy(depth=3)  # budget_ms is None: not budget mode
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=LiveConfig(
                experiment_id="t",
                escalate_after_ply=1,
                escalate_budget_ms=ESCALATED_MS,
            ),
        )
        state = _game(LATE_BOARD, ply=10)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms is None
        assert not state.budget_escalated


class TestBudgetLadder:
    """The 3-tier exponential ladder (briefs/live-pipeline-timing.md §3)."""

    def test_steps_up_base_then_t1_then_t2(self, tmp_path: Path) -> None:
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=_ladder_config(),
        )
        # T0 base: below the median, board wide open.
        state = _game(FRESH_BOARD, ply=LATE_AFTER_PLY - 1)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == BASE_MS
        assert state.budget_tier == 0
        assert not state.budget_escalated
        # T1 late-game: crossed the median, still 14 empty cells (> endgame).
        state.ply = LATE_AFTER_PLY
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == LATE_MS
        assert state.budget_tier == 1
        assert state.budget_escalated
        assert h.runner._pre_escalation_budget_ms == BASE_MS
        # T2 endgame: the board crams down to 2 empty cells (<= 3).
        state.board = LATE_BOARD
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == ENDGAME_MS
        assert state.budget_tier == 2

    def test_early_low_empties_stays_base(self, tmp_path: Path) -> None:
        # The EXACT live bug: a fresh board transiently drops to <= 4 empty
        # around ply 20-40. Without the T2 move-gate that fired the endgame
        # budget at ply 22 and pinned the whole game at the slow budget. The
        # gate (default = the T1 median) must keep an early cramped board at
        # BASE -- it is NOT the death-zone yet.
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=_ladder_config(),
        )
        state = _game(LATE_BOARD, ply=22)  # 2 empty, but only ply 22 -- too early
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == BASE_MS
        assert state.budget_tier == 0
        assert not state.budget_escalated

    def test_late_low_empties_reaches_t2(self, tmp_path: Path) -> None:
        # Same cramped board, but now genuinely late (past the median): the
        # gate opens and T2 fires -- the deepest endgame search.
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=_ladder_config(),
        )
        state = _game(LATE_BOARD, ply=LATE_AFTER_PLY + 1)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == ENDGAME_MS
        assert state.budget_tier == 2

    def test_monotonic_never_steps_down_mid_game(self, tmp_path: Path) -> None:
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=_ladder_config(),
        )
        # Reach T2 (endgame budget).
        state = _game(LATE_BOARD, ply=LATE_AFTER_PLY + 500)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == ENDGAME_MS
        assert state.budget_tier == 2
        # A merge frees up cells (14 empty again) -- but the budget LATCHES at
        # T2: the ladder is monotonic within a game, no step back to T1.
        state.board = FRESH_BOARD
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == ENDGAME_MS
        assert state.budget_tier == 2

    def test_resets_to_base_at_new_game(self, tmp_path: Path) -> None:
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=_ladder_config(),
        )
        state = _game(LATE_BOARD, ply=LATE_AFTER_PLY)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == ENDGAME_MS
        # Starting the next game restores the T0 base budget and a fresh tier.
        new_state = h.runner._begin_game(FRESH_BOARD, t0=0.0)
        assert strat.budget_ms == BASE_MS
        assert new_state.budget_tier == 0
        assert h.runner._pre_escalation_budget_ms is None

    def test_t1_only_when_endgame_tier_unset(self, tmp_path: Path) -> None:
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=LiveConfig(
                experiment_id="t",
                late_after_ply=LATE_AFTER_PLY,
                late_budget_ms=LATE_MS,
            ),
        )
        # Even a cramped board never reaches T2 (endgame knobs are None); once
        # past the median it caps at T1.
        state = _game(LATE_BOARD, ply=LATE_AFTER_PLY)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == LATE_MS
        assert state.budget_tier == 1

    def test_off_by_default_all_knobs_none(self, tmp_path: Path) -> None:
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=LiveConfig(experiment_id="t"),
        )
        state = _game(LATE_BOARD, ply=999_999)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == BASE_MS
        assert state.budget_tier == 0
        assert not state.budget_escalated

    def test_skips_non_budget_go_strategy(self, tmp_path: Path) -> None:
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=ScriptedStrategy([]),
            config=_ladder_config(),
        )
        state = _game(LATE_BOARD, ply=LATE_AFTER_PLY)
        h.runner._maybe_escalate_budget(state)  # must not raise
        assert state.budget_tier == 0
        assert not state.budget_escalated

    def test_inverted_ladder_rejected(self) -> None:
        # T2 below T1 would make the deepest phase shallower -- config raises.
        with pytest.raises(ValueError, match="monotonic ladder"):
            LiveConfig(
                experiment_id="t",
                late_after_ply=LATE_AFTER_PLY,
                late_budget_ms=2000.0,
                endgame_when_empty_le=3,
                endgame_budget_ms=1000.0,
            )

    def test_legacy_escalate_flags_map_onto_ladder(self, tmp_path: Path) -> None:
        # The old single-step knobs resolve through the ladder properties:
        # ply -> T1, empty -> T2, both at escalate_budget_ms.
        strat = _budget_strategy()
        h = make_harness(
            tmp_path,
            FakePhone(board=FRESH_BOARD),
            strategy=strat,
            config=LiveConfig(
                experiment_id="t",
                escalate_after_ply=100,
                escalate_when_empty_le=2,
                escalate_budget_ms=ESCALATED_MS,
            ),
        )
        # Past the ply trigger, board still open -> T1 at the legacy budget.
        state = _game(FRESH_BOARD, ply=100)
        h.runner._maybe_escalate_budget(state)
        assert strat.budget_ms == ESCALATED_MS
        assert state.budget_tier == 1

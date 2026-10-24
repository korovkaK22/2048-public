"""Mid-game search-budget ladder (briefs/live-pipeline-timing.md §3).

Extracted VERBATIM from :class:`~bot2048.runner.live.LiveRunner`: the 3-tier,
monotonic step-up of :class:`~bot2048.ai.go_engine.GoExpectimaxStrategy`'s
``budget_ms`` -- T0 bulk (the base ``--budget-ms``, hidden under the ~250 ms
swipe cadence), T1 late-game (past the ~2067-move champion median, ~2000 ms),
T2 endgame (death-zone, few empty cells, up to ~9000 ms) -- plus the
between-games restore of the T0 base budget. :class:`BudgetLadder` is pure
policy over the strategy's budget setter: no device, no clock, no recording.
The runner calls :meth:`BudgetLadder.maybe_escalate` after every committed
move and :meth:`BudgetLadder.restore_base` when a new game begins; the
per-game tier bookkeeping stays on the runner's ``_GameState``
(``budget_tier`` / ``budget_escalated``) so games remain independent record
attempts. Never raises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from bot2048.ai.go_engine import GoExpectimaxStrategy
from bot2048.ai.strategies import Strategy
from bot2048.runner.live_config import LiveConfig
from bot2048.runner.move_telemetry import _board_stats

if TYPE_CHECKING:
    # Runtime cycle: live.py imports this module, so _GameState (defined there)
    # is pulled in for typing only (annotations are strings under
    # `from __future__ import annotations`).
    from bot2048.runner.live import _GameState

log = structlog.get_logger(__name__)

__all__ = ["BudgetLadder"]

#: Budget-ladder tiers (briefs/live-pipeline-timing.md §3): T0 bulk (base
#: ``--budget-ms``, hidden under the ~250 ms swipe cadence), T1 late-game
#: (past the ~2067-move champion median, ~2000 ms), T2 endgame (death-zone,
#: few empty cells, up to ~9000 ms). Ordered: comparisons drive the
#: monotonic step-up in :meth:`BudgetLadder.maybe_escalate`.
_TIER_BASE: int = 0
_TIER_LATE: int = 1
_TIER_ENDGAME: int = 2


class BudgetLadder:
    """Monotonic 3-tier ``budget_ms`` ladder for one live session.

    Owned by :class:`~bot2048.runner.live.LiveRunner`, which injects its
    strategy and config. :attr:`pre_escalation_budget_ms` is the go strategy's
    pre-escalation (T0 base) ``budget_ms``, remembered when a game first steps
    up so the NEXT game starts back at the base budget (games stay independent
    attempts); ``None`` while no game has escalated.
    """

    def __init__(self, *, strategy: Strategy, config: LiveConfig) -> None:
        self.strategy = strategy
        self.config = config
        self.pre_escalation_budget_ms: float | None = None

    def maybe_escalate(self, state: _GameState) -> None:
        """Step the go strategy's ``budget_ms`` UP the 3-tier ladder (brief §3).

        Each committed move the target tier is computed from the CURRENT
        phase -- T2 endgame when the board has at most
        ``ladder_endgame_when_empty_le`` empty cells (death-zone, near
        cash-out), else T1 late-game once ``state.ply`` reached
        ``ladder_late_after_ply`` (the ~2067-move champion median), else T0
        base -- and applied via :meth:`GoExpectimaxStrategy.set_budget_ms`
        exactly when the tier RISES. Monotonic within a game: a merge that
        frees empty cells (or any phase flapping) never steps the budget back
        down; a fresh ``_GameState`` starts back at T0 and
        :meth:`restore_base` restores the base budget. The legacy one-shot
        ``escalate_*`` knobs map onto the ladder in
        :class:`~bot2048.runner.live_config.LiveConfig` (ply trigger -> T1,
        empty trigger -> T2, both at ``escalate_budget_ms``), so old flags
        behave exactly as before. OFF (no-op) when no tier is configured;
        silently skipped when the strategy is not a budget-mode
        :class:`GoExpectimaxStrategy`. Never raises.
        """
        strategy = self.strategy
        if not isinstance(strategy, GoExpectimaxStrategy):
            return
        if strategy.budget_ms is None:
            return  # fixed-depth mode: the ladder is a budget-mode lever only
        tier, budget_ms, empty = self.target(state)
        if budget_ms is None or tier <= state.budget_tier:
            return  # monotonic: never step down (or re-apply) mid-game
        if self.pre_escalation_budget_ms is None:
            self.pre_escalation_budget_ms = strategy.budget_ms
        strategy.set_budget_ms(budget_ms)
        state.budget_tier = tier
        state.budget_escalated = True
        log.info(
            "live.budget_escalated",
            game_id=state.game_id,
            ply=state.ply,
            empty=empty,
            tier=tier,
            new_budget_ms=budget_ms,
        )

    def target(self, state: _GameState) -> tuple[int, float | None, int]:
        """The budget-ladder tier the CURRENT phase calls for (brief §3).

        Returns ``(tier, budget_ms, empty_cells)``: T2 (endgame) when the
        board is in the death-zone AND the game is genuinely late, else T1
        (late) when the ply trigger fires, else T0 with ``None`` (= the base
        budget; the caller never steps to it, base is only restored between
        games). A tier whose threshold or budget knob is ``None`` is disabled
        and can never be the target.

        T2 is gated by BOTH few empty cells (``ladder_endgame_when_empty_le``)
        AND a move-count floor (``ladder_endgame_after_ply``, defaulting to the
        T1 median): a fresh board transiently dips to <= 4 empty around ply
        20-40, so an empties-ONLY endgame trigger fired at ply 22 in a live run
        and pinned the whole game at the slow budget. Requiring the game to be
        late first makes low-empties mean "endgame" only when it truly is.
        """
        cfg = self.config
        empty = _board_stats(state.board).empty
        endgame_budget = cfg.ladder_endgame_budget_ms
        endgame_empty_le = cfg.ladder_endgame_when_empty_le
        endgame_after_ply = cfg.ladder_endgame_after_ply
        if (
            endgame_budget is not None
            and endgame_empty_le is not None
            and empty <= endgame_empty_le
            and (endgame_after_ply is None or state.ply >= endgame_after_ply)
        ):
            return _TIER_ENDGAME, endgame_budget, empty
        late_budget = cfg.ladder_late_budget_ms
        late_after_ply = cfg.ladder_late_after_ply
        if (
            late_budget is not None
            and late_after_ply is not None
            and state.ply >= late_after_ply
        ):
            return _TIER_LATE, late_budget, empty
        return _TIER_BASE, None, empty

    def restore_base(self) -> None:
        """Put the strategy back on the T0 base budget before a new game.

        A previous game's budget-ladder step (brief §3) must not leak into the
        next one: every game is an independent record attempt, so the
        remembered pre-escalation ``budget_ms`` is re-applied and forgotten
        (the caller's fresh ``_GameState`` resets the tier to T0). No-op when
        no game escalated or the strategy is not a :class:`GoExpectimaxStrategy`.
        """
        if self.pre_escalation_budget_ms is not None and isinstance(
            self.strategy, GoExpectimaxStrategy
        ):
            self.strategy.set_budget_ms(self.pre_escalation_budget_ms)
            log.debug(
                "live.budget_restored", budget_ms=self.pre_escalation_budget_ms
            )
            self.pre_escalation_budget_ms = None

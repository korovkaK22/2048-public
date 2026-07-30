"""Research mode: measure the monobank clone's spawn rules empirically.

The clone's randomness is undocumented (docs/rules-research.md): 2-vs-4
spawn probability, number of initial tiles, uniformity over empty cells.
Research mode answers these with volume, not score:

1.  Every game starts through the calibrated restart flow
    (``Supervisor.start_new_game``: the NEW_GAME -> CONFIRM_YES taps), so the
    parsed initial board is a genuinely fresh one. Its tiles are recorded as
    ``TileSpawned`` with negative plies (first tile ply ``-1``, next ``-2``,
    recording contract) -- this measures *initial tile count and values*.
2.  Moves come from a cheap policy (``random`` by default, ``greedy``
    optionally -- see :mod:`bot2048.ai.strategies`). For each move the live
    loop predicts the after-move board with ``core.engine.move``, parses the
    next verified screenshot, and the single cell that is empty in the
    prediction but occupied in the parse is the spawn
    (:func:`bot2048.runner.live.diff_spawn`) -> ``TileSpawned`` (cell +
    exponent). Any other diff is a parse/animation error: the capture is
    retried, and a persistently contradictory board aborts the game with
    ``end_reason='error'`` (the sample is dropped, never guessed).
3.  Repeat for :attr:`ResearchConfig.games` games, then analyze the
    ``spawns`` table over SQL (``bot2048 db``): empirical P(2)/P(4),
    chi-square for uniformity over empty cells, per-ply drift.

The result becomes a measured :class:`bot2048.core.types.SpawnModel`
(replacing the classic 90/10 default) so simulations match the real app.

Implementation note: research IS the live loop with a cheap policy --
:class:`ResearchRunner` delegates to :class:`bot2048.runner.live.LiveRunner`
(``fresh_start=True``), so swipe verification, escalation, auth handling and
recording behave identically in both modes and are tested once.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from bot2048.ai.strategies import GreedyStrategy, RandomStrategy, Strategy
from bot2048.device.humanizer import BBox, Humanizer
from bot2048.recording import Recorder
from bot2048.runner.alerts import AlertWriter
from bot2048.runner.incidents import MoveRing
from bot2048.runner.live import (
    BoardReader,
    GameSupervisor,
    HeartbeatSink,
    LiveConfig,
    LiveDevice,
    LiveRunner,
    NotifyHook,
    StateDetector,
)
from bot2048.storage.ops_schema import OpsRepository
from bot2048.tgnotify import PAUSE_FLAG_PATH

log = structlog.get_logger(__name__)

__all__ = ["ResearchConfig", "ResearchRunner", "make_policy"]


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    """Parameters of a spawn-statistics collection session.

    Attributes:
        experiment_id: groups the recorded games for later analysis.
        games: how many full games to collect (more games = tighter
            confidence interval on P(4); ~1000 spawns gives +/-2-3%).
        move_policy: ``"random"`` | ``"greedy"`` -- cheap volume generators.
        settle_delay_s: post-swipe animation wait, as in live mode.
    """

    experiment_id: str = "spawn-research"
    games: int = 20
    move_policy: str = "random"
    settle_delay_s: float = 0.35

    def __post_init__(self) -> None:
        if self.games < 1:
            raise ValueError(f"games must be >= 1, got {self.games}")


def make_policy(name: str, rng: random.Random | None = None) -> Strategy:
    """Cheap move policy for volume collection. Raises ValueError on unknown."""
    if name == "random":
        return RandomStrategy(rng)
    if name == "greedy":
        return GreedyStrategy()
    raise ValueError(f"unknown move_policy {name!r} (expected 'random' or 'greedy')")


class ResearchRunner:
    """Collects spawn statistics from the real app into the games DB.

    Thin wrapper: builds a :class:`LiveRunner` with a cheap policy and
    ``fresh_start=True`` (every game begins via the restart flow, so
    initial-tile statistics are clean).
    """

    def __init__(
        self,
        *,
        device: LiveDevice,
        detect_state: StateDetector,
        read_board: BoardReader,
        recorder: Recorder,
        supervisor: GameSupervisor,
        alerts: AlertWriter,
        grid_bbox: BBox,
        humanizer: Humanizer | None = None,
        config: ResearchConfig | None = None,
        run_id: str | None = None,
        ops: OpsRepository | None = None,
        heartbeat: HeartbeatSink | None = None,
        ring: MoveRing | None = None,
        notify: NotifyHook | None = None,
        pause_flag: Path = PAUSE_FLAG_PATH,
        rng: random.Random | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or ResearchConfig()
        policy = make_policy(self.config.move_policy, rng)
        live_config = LiveConfig(
            experiment_id=self.config.experiment_id,
            strategy_name=policy.name,
            max_games=self.config.games,
            settle_delay_s=self.config.settle_delay_s,
            fresh_start=True,
        )
        self._live = LiveRunner(
            device=device,
            detect_state=detect_state,
            read_board=read_board,
            strategy=policy,
            recorder=recorder,
            supervisor=supervisor,
            alerts=alerts,
            grid_bbox=grid_bbox,
            humanizer=humanizer,
            config=live_config,
            run_id=run_id,
            ops=ops,
            heartbeat=heartbeat,
            ring=ring,
            notify=notify,
            pause_flag=pause_flag,
            sleep=sleep,
            clock=clock,
        )

    @property
    def live(self) -> LiveRunner:
        """The underlying live loop (exposed for tests/inspection)."""
        return self._live

    def run(self) -> None:
        """Collect spawn samples for :attr:`ResearchConfig.games` games."""
        log.info(
            "research.session_started",
            games=self.config.games,
            move_policy=self.config.move_policy,
            experiment_id=self.config.experiment_id,
        )
        self._live.run()
        log.info("research.session_done", games=self.config.games)

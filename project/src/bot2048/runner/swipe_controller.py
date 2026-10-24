"""One-move execution for the live loop (R7, docs/archive/refactor-plan.md).

The last and thinnest extraction from :class:`~bot2048.runner.live.LiveRunner`:
:class:`SwipeController` owns the choose -> swipe -> verify -> commit path for
ONE move, plus its auto-learn (vision self-heal) and per-move / per-game
telemetry. ``LiveRunner`` holds one and calls :meth:`play_move` per ply from
``_play_game``. The extraction is behavior-preserving (0 change):

* the swipe verify-and-retry loop is byte-identical -- ``swipe_retries + 1``
  attempts, the ``swipe_retry_delay_s`` pause between them, the
  ``live.swipe_unregistered`` warn and the MOVE_REJECTED escalation once the
  attempts are exhausted (``end_reason='move_rejected'``);
* the shared :class:`~bot2048.runner.move_telemetry._MovePhaseTimer` /
  :class:`~bot2048.runner.move_telemetry._MoveCounters` are the runner's OWN
  instances (exactly as the gate shares them), so the ``timing`` / ``move``
  ``eval_details`` buckets (``swipe_ms`` / ``swipe_attempts`` / ``capture_count``
  / ``gate_hit`` / ``perceive_ms`` / ``settle_ms`` / ``total_ms``) stay
  byte-identical -- the de-facto ``db latency`` read contract;
* verification is delegated to the injected :class:`ObservationGate`
  (``observe_after_swipe``); the ``spawn`` / ``mismatch`` / ``unchanged``
  branches are unchanged;
* auto-learn (:meth:`_try_learn_new_tiles`) trusts the engine over the
  recognizer under the exact same narrow conditions (merge-target cross-check +
  a suspect cell + an accepted color sample);
* the spawn diff (:func:`~bot2048.runner.outcomes.diff_spawn`) and the
  ``MovePlayed`` / ``TileSpawned`` recording are unchanged (``recording.py`` is
  frozen -- event signatures untouched);
* ``note_move_committed`` (the auth popup loop-guard reset) still fires on every
  committed move, via the injected callback -- never lost.

``clock`` / ``sleep`` are passed as thin accessors of the runner's CURRENT
attributes (not bound once), matching the gate / auth / recovery extractions, so
a test that swaps ``runner._clock`` / ``runner._sleep`` after construction is
honored exactly as before.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from bot2048.ai.go_engine import GoEngineError
from bot2048.core import engine
from bot2048.core.types import Spawn
from bot2048.device.humanizer import BBox, Humanizer
from bot2048.recording import GameEvent, MovePlayed, TileSpawned
from bot2048.runner.alerts import AlertCode
from bot2048.runner.errors import LiveLoopError
from bot2048.runner.incidents import Image, MoveRing
from bot2048.runner.live_config import _GATE_SPAWN_EXPONENTS, LiveConfig
from bot2048.runner.move_telemetry import (
    _SETTLE,
    _SWIPE,
    _MoveCounters,
    _MovePhaseTimer,
)
from bot2048.runner.outcomes import (
    WIN_EXPONENT,
    _HeldBeforeWin,
    _merge_targets,
    diff_spawn,
)
from bot2048.storage.schema import board_to_hex

if TYPE_CHECKING:
    from bot2048.ai.strategies import Decision, Strategy
    from bot2048.core.types import Board, MoveResult
    from bot2048.runner.debug_capture import DebugCapture
    from bot2048.runner.live import (
        BoardSource,
        CellsReader,
        GameSupervisor,
        HeartbeatSink,
        LearnTileHook,
        LiveDevice,
        _GameState,
    )
    from bot2048.runner.observation import ObservationGate

log = structlog.get_logger(__name__)


class SwipeController:
    """Choose / swipe / verify / commit one move for a live session.

    All collaborators are injected by :class:`~bot2048.runner.live.LiveRunner`;
    the timer / counters / gate are the runner's own instances (shared, not
    copies) so the telemetry buckets and the gate's ``last_frame`` stay
    byte-identical to the pre-extraction loop.
    """

    def __init__(
        self,
        *,
        strategy: Strategy,
        device: LiveDevice,
        grid_bbox: BBox,
        humanizer: Humanizer,
        gate: ObservationGate,
        config: LiveConfig,
        supervisor: GameSupervisor,
        timer: _MovePhaseTimer,
        counters: _MoveCounters,
        board_source: BoardSource | None,
        read_cells: CellsReader | None,
        learn_tile: LearnTileHook | None,
        heartbeat: HeartbeatSink | None,
        ring: MoveRing | None,
        record: Callable[[GameEvent], None],
        notify: Callable[[str, str, str], None],
        strategy_label: Callable[[], str],
        control_tick: Callable[[_GameState], None],
        note_move_committed: Callable[[], None],
        clock: Callable[[], float],
        sleep: Callable[[float], Any],
        debug_capture: DebugCapture | None = None,
    ) -> None:
        self.strategy = strategy
        self.device = device
        self.grid_bbox = grid_bbox
        self.humanizer = humanizer
        self.gate = gate
        self.config = config
        self.supervisor = supervisor
        self._timer = timer
        self._counters = counters
        self.board_source = board_source
        self.read_cells = read_cells
        self.learn_tile = learn_tile
        self.heartbeat = heartbeat
        self.ring = ring
        self._record = record
        self._notify = notify
        self._strategy_label = strategy_label
        self._control_tick = control_tick
        self._note_move_committed = note_move_committed
        self._clock = clock
        self._sleep = sleep
        self.debug_capture = debug_capture
        #: Wall cost (ms) of the PREVIOUS committed move's persist section
        #: (cadence log + MovePlayed/TileSpawned record + ring); stashed here
        #: and written into the NEXT move's ``eval_details["move"]`` as
        #: ``prev_commit_ms`` (design-strategy-logging §5.1 -- the recorder-cost
        #: probe that isolates the DB/log tail from the vision snapshot).
        self._prev_commit_ms: float | None = None

    # -- one move ---------------------------------------------------------------

    def play_move(self, state: _GameState) -> str | None:
        """Choose, swipe, verify, record one move.

        Returns ``None`` when the move was verified on screen and recorded;
        otherwise a SPECIFIC abort reason (``"vision_mismatch"`` /
        ``"move_rejected"``) that becomes the game's ``end_reason``. Such an
        abort means the capture/verification could not continue -- the game did
        not truly end and no code bug fired -- so it must NOT be recorded as a
        generic ``'error'``.

        Raises:
            _HeldBeforeWin: ``--stop-before-win`` is set and the chosen move
                would merge to 2048 -- the swipe is withheld and the runner
                finalizes the game as ``'held_before_win'`` (a NORMAL stop).
        """
        self._timer.reset()  # phase telemetry of THIS move starts here
        self._counters.reset()  # reliability counters of THIS move too
        self._control_tick(state)
        decision: Decision = self._choose_with_retry(state)
        predicted = engine.move(state.board, decision.direction)
        if not predicted.moved:
            raise LiveLoopError(
                f"strategy {self._strategy_label()!r} chose illegal move "
                f"{decision.direction.name} on {board_to_hex(state.board)}"
            )
        # Safe "brink" mode (--stop-before-win): if the move the strategy just
        # picked WOULD create a 2048 tile, hold the board one swipe short so a
        # human makes the final merge. Reuses the ALREADY-computed ``predicted``
        # board (one max-exponent read) -- zero hot-path latency, no extra engine
        # call or capture. The runner catches this and finalizes cleanly.
        if self.config.stop_before_win and engine.max_exponent(predicted.board) >= WIN_EXPONENT:
            raise _HeldBeforeWin(decision.direction)
        attempts = self.config.swipe_retries + 1
        for attempt in range(1, attempts + 1):
            with self._timer.phase(_SWIPE):
                self.device.swipe_direction(decision.direction, self.grid_bbox, self.humanizer)
            self._counters.swipe_attempts = attempt
            self._settle_sleep(self.config.settle_delay_s + self.humanizer.next_delay())
            outcome, parsed = self.gate.observe_after_swipe(state, predicted.board)
            if outcome == "spawn":
                spawn = diff_spawn(predicted.board, parsed)
                assert spawn is not None  # guaranteed by outcome
                self._commit_move(state, decision, predicted, spawn, parsed)
                return None
            if outcome == "mismatch":
                # Self-healing recognition: a freshly merged tile the
                # recognizer does not know yet is TAUGHT from the engine
                # prediction instead of aborting (see _try_learn_new_tiles).
                spawn = self._try_learn_new_tiles(state, decision, predicted, parsed)
                if spawn is not None:
                    corrected = list(predicted.board)
                    corrected[spawn.cell] = spawn.exponent
                    self._commit_move(state, decision, predicted, spawn, tuple(corrected))
                    return None
                # Transient physical occlusion (e.g. a cat's paw over 1-2 cells)
                # parses as a mismatch. If the accepted frame's cells are
                # UNREADABLE (color-fallback / low-confidence) rather than a
                # clean-but-different board, wait the occlusion out with a slow
                # fresh-recapture retry before aborting -- occlusion is
                # transient and the leaderboard ranks score, not time
                # (:meth:`_occlusion_retry`). A fully-readable but genuinely
                # different board is a REAL mismatch and aborts at once.
                if self._frame_is_occluded(predicted.board):
                    verdict = self._occlusion_retry(state, decision, predicted, parsed)
                    if verdict == "committed":
                        return None
                    if verdict == "unregistered":
                        # A fresh trusted read shows the PRE-swipe board once the
                        # occlusion cleared: the swipe never registered -> fall
                        # into the existing swipe-retry path (unchanged branch).
                        log.warning(
                            "live.swipe_unregistered",
                            attempt=attempt,
                            of=attempts,
                            direction=decision.direction.name,
                            game_id=state.game_id,
                            ply=state.ply,
                        )
                        if attempt < attempts:
                            self._settle_sleep(self.config.swipe_retry_delay_s)
                        continue
                    # verdict == "mismatch": the occlusion never cleared (or a
                    # trusted frame confirmed a genuinely different board) --
                    # fall through to the standard VISION_MISMATCH abort below.
                # Persistent contradiction: the screen shows a board that is
                # neither the old one nor a valid successor. ERROR alert +
                # one supervisor recovery cycle, then abort -- the recorded
                # game must stay consistent (never guess the spawn).
                # Debug bundle FIRST (fire-and-forget, off-thread): the screen
                # still shows the offending state here, before the supervisor's
                # recovery cycle touches it. This is the primary diagnostic for
                # every vision_mismatch abort (mascot? animation? vision bug?).
                self._debug_snap(
                    "vision_mismatch",
                    state,
                    attempt=attempt,
                    direction=decision.direction.name,
                    board_before=board_to_hex(state.board),
                    board_predicted=board_to_hex(predicted.board),
                    board_parsed=board_to_hex(parsed),
                    suspect_cells=self._occluded_cells(predicted.board),
                )
                self.supervisor.handle_error(
                    AlertCode.VISION_MISMATCH,
                    f"parsed board is neither pre-move nor predicted+spawn after "
                    f"{self.config.parse_retries} captures (move {decision.direction.name})",
                    game_id=state.game_id,
                    move_no=state.ply,
                    board_hex=board_to_hex(parsed),
                    attempt=attempt,
                    extra={
                        "board_before": board_to_hex(state.board),
                        "board_predicted": board_to_hex(predicted.board),
                        "direction": decision.direction.name,
                    },
                )
                return "vision_mismatch"
            # outcome == "unchanged": the swipe did not register; retry it.
            log.warning(
                "live.swipe_unregistered",
                attempt=attempt,
                of=attempts,
                direction=decision.direction.name,
                game_id=state.game_id,
                ply=state.ply,
            )
            if attempt < attempts:
                self._settle_sleep(self.config.swipe_retry_delay_s)
        board_before_hex = board_to_hex(state.board)
        self.supervisor.handle_error(
            AlertCode.MOVE_REJECTED,
            f"swipe {decision.direction.name} rejected {attempts}x in a row "
            f"(board unchanged after every attempt) -- game {state.game_id} "
            f"ply {state.ply}, retry delay {self.config.swipe_retry_delay_s:g}s; "
            f"board before move: {board_before_hex}",
            game_id=state.game_id,
            move_no=state.ply,
            board_hex=board_before_hex,
            attempt=attempts,
            extra={
                "direction": decision.direction.name,
                "attempts": attempts,
                "swipe_retry_delay_s": self.config.swipe_retry_delay_s,
                "board_before": board_before_hex,
                "board_predicted": board_to_hex(predicted.board),
            },
        )
        return "move_rejected"

    def _choose_with_retry(self, state: _GameState) -> Decision:
        """Ask the strategy for a move, surviving ONE engine hiccup.

        A single :class:`~bot2048.ai.go_engine.GoEngineError` (a subprocess I/O
        error, an unexpected exit, a malformed reply) currently finalizes the
        whole game as ``'error'`` -- throwing away a possibly deep,
        record-track game over a transient pipe glitch. Restart the engine
        (:meth:`GoExpectimaxStrategy.close` -> the next ``choose()`` re-spawns
        the subprocess) and retry the decision exactly ONCE; only a SECOND
        failure propagates and finalizes the game. ``close()`` is called
        defensively (strategies without it -- pure-Python ones never raise
        :class:`GoEngineError` anyway -- are left untouched) and its own
        failure never masks the retry.
        """
        try:
            return self.strategy.choose(state.board)
        except GoEngineError as exc:
            log.warning(
                "live.choose_retry",
                error=str(exc),
                game_id=state.game_id,
                ply=state.ply,
            )
            close = getattr(self.strategy, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as close_exc:  # cleanup must not mask the retry
                    log.warning("live.choose_close_failed", error=str(close_exc))
            return self.strategy.choose(state.board)  # a 2nd failure aborts the game

    # -- transient-occlusion slow retry ---------------------------------------------

    def _occluded_cells(
        self, predicted_board: Board, frame: Image | None = None
    ) -> list[int]:
        """Cells where the PREDICTED board expects a tile but vision could not
        read it cleanly on ``frame`` -- the genuine-occlusion signal.

        A :class:`CellLook` is ``suspect`` when the cell was decided WITHOUT a
        clean digit read. Crucially, an EMPTY board cell is ALSO ``suspect``
        (there is no digit to read), so a raw suspect list is dominated by
        empty cells and is worthless as an occlusion signal -- LIVE 2026-07-11:
        6-7 EMPTY cells flagged "occluded" burned ~20 s of retries and aborted
        healthy games at ply 361/365. We therefore keep only suspect cells that
        the just-played ``predicted_board`` (after-swipe, pre-spawn) expects to
        HOLD a tile: a tile that should be there but reads unreadable = a real
        occluder (the game's cat-mascot paw, a physical cover). Empty predicted
        cells that read suspect are NOT occlusion.

        ``frame`` defaults to the gate's :attr:`~ObservationGate.last_frame`.
        Empty when no per-cell detail is available (no :class:`CellsReader`, no
        frame, or a raising detailed parse) -- the caller then keeps today's
        immediate abort.
        """
        if frame is None:
            frame = self.gate.last_frame
        if self.read_cells is None or frame is None:
            return []
        looks = self.read_cells(frame)
        if looks is None:
            return []
        return [
            cell
            for cell, look in enumerate(looks)
            if look.suspect and predicted_board[cell] != 0
        ]

    def _frame_is_occluded(self, predicted_board: Board) -> bool:
        """True when the just-mismatched frame has a genuinely occluded TILE (a
        predicted-non-empty cell vision could not read)."""
        return bool(self._occluded_cells(predicted_board))

    def _occlusion_retry(
        self,
        state: _GameState,
        decision: Decision,
        predicted: MoveResult,
        parsed: Board,
    ) -> str:
        """Wait out a transient occlusion with slow fresh-recapture retries.

        Up to :attr:`LiveConfig.occlusion_max_retries` iterations, each logging
        ``live.vision_occluded_retry`` (attempt / elapsed / occluded cells),
        sleeping :attr:`LiveConfig.occlusion_retry_delay_s`, then forcing a
        BRAND-NEW frame capture (via the gate, bypassing any stale APK mailbox
        packet) and re-running the SAME verification. Returns:

        * ``"committed"`` -- a fully-readable fresh frame reconciled to the
          predicted-after-swipe board; the move was recorded here.
        * ``"unregistered"`` -- a fully-readable fresh frame shows the PRE-swipe
          board (the swipe never registered); the caller re-swipes.
        * ``"mismatch"`` -- every retry stayed occluded/untrusted, or a trusted
          frame confirmed a genuinely different board: give up
          (``live.vision_occluded_giveup``) and let the caller abort.

        The occlusion waits are billed to :attr:`self._sleep` directly, NOT the
        per-move SETTLE phase timer -- a ~15 s outage must not inflate the
        move's ``settle_ms`` latency telemetry.
        """
        cfg = self.config
        start = self._clock()
        occluded = self._occluded_cells(predicted.board)
        last_parsed: Board = parsed
        for attempt in range(1, cfg.occlusion_max_retries + 1):
            log.warning(
                "live.vision_occluded_retry",
                attempt=f"{attempt}/{cfg.occlusion_max_retries}",
                elapsed_s=round(self._clock() - start, 2),
                occluded_cells=occluded,
                direction=decision.direction.name,
                game_id=state.game_id,
                ply=state.ply,
            )
            if attempt == 1:
                # The highest-value debug moment: the FIRST retry, while the
                # occluder (mascot / paw / animation) should still be on
                # screen. Fire-and-forget -- the screencap runs on a worker
                # thread while this loop sleeps out the occlusion.
                self._debug_snap(
                    "occluded",
                    state,
                    phase="first_retry",
                    direction=decision.direction.name,
                    occluded_cells=list(occluded),
                    board_before=board_to_hex(state.board),
                    board_predicted=board_to_hex(predicted.board),
                    board_parsed=board_to_hex(last_parsed),
                )
            self._sleep(cfg.occlusion_retry_delay_s)
            frame = self.gate.capture_game_frame(state)  # fresh, fail-closed
            fresh = self.gate.parse_or_escalate(frame, state=state)
            last_parsed = fresh
            occluded = self._occluded_cells(predicted.board, frame)
            if not occluded:  # a fully-readable (trusted) frame
                spawn = diff_spawn(predicted.board, fresh)
                if spawn is not None and spawn.exponent in _GATE_SPAWN_EXPONENTS:
                    self._commit_move(state, decision, predicted, spawn, fresh)
                    return "committed"
                if fresh == state.board:
                    return "unregistered"
                # Readable but neither successor nor pre-move: a REAL mismatch,
                # not an occlusion. Stop waiting and abort.
                break
        log.warning(
            "live.vision_occluded_giveup",
            elapsed_s=round(self._clock() - start, 2),
            attempts=cfg.occlusion_max_retries,
            occluded_cells=occluded,
            direction=decision.direction.name,
            game_id=state.game_id,
            ply=state.ply,
        )
        # The occlusion never cleared: capture the giving-up moment too (what
        # does the screen show after ~15 s? still the mascot? a new screen?).
        self._debug_snap(
            "occluded",
            state,
            phase="giveup",
            attempts=cfg.occlusion_max_retries,
            direction=decision.direction.name,
            occluded_cells=list(occluded),
            board_before=board_to_hex(state.board),
            board_predicted=board_to_hex(predicted.board),
            board_parsed=board_to_hex(last_parsed),
        )
        return "mismatch"

    def _try_learn_new_tiles(
        self,
        state: _GameState,
        decision: Decision,
        predicted: MoveResult,
        parsed: Board,
    ) -> Spawn | None:
        """Auto-learn gate: trust the ENGINE over the recognizer -- narrowly.

        Trigger condition (ALL must hold, otherwise ``None`` and the real
        VISION_MISMATCH abort stands):

        * the settled parsed board differs from the engine prediction at
          exactly one plausible spawn (a predicted-EMPTY cell now holding a
          tile) plus one or more "learnable" cells;
        * every learnable cell is a merge TARGET of this very move whose
          predicted exponent equals the merged pair + 1 (value = 2x the
          pair) and is HIGHER than what vision parsed there;
        * vision itself marked each such cell suspect (color fallback /
          low confidence) via the injected :class:`CellsReader`;
        * :attr:`learn_tile` accepts the color sample from the settled
          frame for every learnable cell (validated in vision, never
          poisons the palette).

        On success the tile colors are learned (in-memory calibration
        swapped + persisted by the hook), ``live.tile_learned`` is logged
        per cell and the verified spawn is returned so the move commits
        with the predicted board -- the bot climbs 512 -> 1024 -> 2048 -> ...
        without a human recalibrating.
        """
        frame = self.gate.last_frame
        if self.read_cells is None or self.learn_tile is None or frame is None:
            return None
        looks = self.read_cells(frame)
        if looks is None or len(looks) != len(parsed):
            return None
        merged = _merge_targets(state.board, decision.direction)
        learn: list[int] = []
        spawn: Spawn | None = None
        for cell, (p, r) in enumerate(zip(predicted.board, parsed, strict=True)):
            if p == r:
                continue
            if p == 0 and r > 0 and spawn is None:
                spawn = Spawn(cell=cell, exponent=r)
                continue
            if p > r and merged.get(cell) == p and looks[cell].suspect:
                learn.append(cell)  # plausible freshly-merged unknown tile
                continue
            return None  # any other diff: genuine misread, do NOT paper over
        if spawn is None or not learn:
            return None
        for cell in learn:
            try:
                accepted = self.learn_tile(frame, cell, predicted.board[cell])
            except Exception as exc:  # hook trouble = refuse, keep the abort
                log.warning("live.tile_learn_failed", cell=cell, error=str(exc))
                return None
            if not accepted:
                log.warning(
                    "live.tile_learn_refused",
                    cell=cell,
                    exponent=predicted.board[cell],
                    game_id=state.game_id,
                    ply=state.ply,
                )
                return None
            log.info(
                "live.tile_learned",
                exponent=predicted.board[cell],
                value=2 ** predicted.board[cell],
                cell=cell,
                game_id=state.game_id,
                ply=state.ply,
            )
        learned_values = ", ".join(str(2 ** predicted.board[cell]) for cell in learn)
        self._notify(
            "INFO",
            "TILE_LEARNED",
            f"🎓 Донавчив нову плитку: {learned_values} "
            f"(клітинки {', '.join(str(c) for c in learn)}; кроп у logs/learned_tiles/)",
        )
        return spawn

    def _commit_move(
        self,
        state: _GameState,
        decision: Decision,
        predicted: MoveResult,
        spawn: Spawn,
        parsed: Board,
    ) -> None:
        now = time.time()
        # Monotonic commit stamp: ``db latency`` pairs consecutive plies of one
        # game (``gap_ms = t_commit[n] - t_commit[n-1]``) and subtracts the
        # critical path of move n to expose the UNACCOUNTED loop tail --
        # recorder flush, vision snapshot, ladder, control tick -- that the
        # phase timer never bills. Same process for a whole game, so the
        # monotonic origin is irrelevant; a float, no I/O.
        t_commit = time.monotonic()
        score_total = state.score_total + predicted.score_gained
        # Per-move phase breakdown rides in the free-form eval_details bucket
        # (frozen recording contract untouched). The strategy's own dict is
        # copied, never mutated, and its keys (engine_ms, prefetch_hit, ...)
        # are preserved -- the read side derives IPC = eval_ms - engine_ms.
        timing = self._timer.snapshot()
        details: dict[str, Any] = dict(decision.details)
        details["timing"] = timing
        # Reliability sidecars (docs/research/telemetry_review.md M1-M3): capture/
        # swipe counts + predict-gate outcome + FACTUAL capture_kind, and the
        # per-cell vision-fallback picture -- both free-form neighbors of the
        # frozen ``timing`` bucket, never inside it.
        move_counts = self._counters.snapshot()
        # Latency-gap probes (design-strategy-logging §5.1): additive neighbours
        # of the frozen ``timing`` bucket, same pattern as ``apk_age_ms``.
        # ``prev_commit_ms`` is omitted on ply 0 -- the stashed value would
        # belong to the previous GAME's last move.
        move_counts["t_commit"] = t_commit
        if state.ply > 0 and self._prev_commit_ms is not None:
            move_counts["prev_commit_ms"] = self._prev_commit_ms
        details["move"] = move_counts
        vision = self._vision_snapshot()
        if vision is not None:
            details["vision"] = vision
        self._accumulate_game_telemetry(state, timing, vision, parsed)
        t_persist = time.monotonic()
        # INFO (not DEBUG): the health-monitor greps this every
        # move for the cadence + swipe-miss signal, so it MUST survive the
        # default INFO run level (docs/ops-monitoring.md). It is the ONE
        # per-move line kept at INFO -- everything else per move is DEBUG.
        log.info(
            "live.move_timing",
            game_id=state.game_id,
            ply=state.ply,
            gate_hit=self._counters.gate_hit,
            capture_count=self._counters.capture_count,
            swipe_attempts=self._counters.swipe_attempts,
            **{key: round(value, 2) for key, value in timing.items()},
        )
        self._record(
            MovePlayed(
                game_id=state.game_id,
                ply=state.ply,
                direction=decision.direction,
                board_before=state.board,
                board_after=predicted.board,
                score_gained=predicted.score_gained,
                score_total=score_total,
                eval_ms=decision.eval_ms,
                search_depth=decision.search_depth,
                eval_details=details,
                ts=now,
            )
        )
        self._record(
            TileSpawned(
                game_id=state.game_id,
                ply=state.ply,
                spawn=spawn,
                board_after=parsed,
                ts=now,
            )
        )
        if self.ring is not None:
            self.ring.record_move(
                ply=state.ply,
                direction=decision.direction.name,
                board_before=board_to_hex(state.board),
                board_after=board_to_hex(parsed),
                eval_ms=decision.eval_ms,
                score_total=score_total,
            )
        # The persist section just measured (log + 2x record + ring) is THIS
        # move's cost; it rides in the NEXT move's row as ``prev_commit_ms``.
        self._prev_commit_ms = (time.monotonic() - t_persist) * 1000.0
        state.board = parsed
        # Genuine progress: a move committed on a healthy board, so any prior
        # auth->terminal-popup restart streak did NOT stall. Clear the loop-guard
        # so only BACK-TO-BACK popup restarts with no move between them (the
        # true stuck loop) ever reach its FATAL (see AuthFlow.handle_auth).
        self._note_move_committed()
        state.score_total = score_total
        state.ply += 1
        if self.heartbeat is not None:
            self.heartbeat.update(game_id=state.game_id, ply=state.ply)

    def _vision_snapshot(self) -> dict[str, Any] | None:
        """Per-move vision-fallback picture (docs/research/telemetry_review.md M1).

        Counts the cells the recognizer resolved WITHOUT a clean digit read
        (``CellLook.suspect`` -- color fallback / low confidence) on the
        accepted frame, plus their 16-bit mask and the exponents that were
        unreadable -- the early predictor of a silent palette drift over a
        50k-move campaign, and a hint at WHICH tile values to recalibrate.

        ``None`` (bucket omitted) when detailed per-cell reads are unavailable:
        no injected :class:`CellsReader`, the fast APK path (no frame parsed
        this move), no frame behind the latest parse, or a raising detailed
        parse. Costs one detailed parse of the already-captured accepted frame,
        outside the timed phases, and only on committed moves.
        """
        if self.read_cells is None or self.board_source is not None:
            return None
        frame = self.gate.last_frame
        if frame is None:
            return None
        try:
            looks = self.read_cells(frame)
        except Exception as exc:  # a telemetry probe must never break the move
            log.warning("live.vision_snapshot_failed", error=str(exc))
            return None
        if looks is None:
            return None
        suspect = 0
        mask = 0
        exponents: list[int] = []
        for cell, look in enumerate(looks):
            if look.suspect:
                suspect += 1
                mask |= 1 << cell
                exponents.append(look.exponent)
        return {"suspect": suspect, "suspect_mask": mask, "suspect_exps": exponents}

    def _accumulate_game_telemetry(
        self,
        state: _GameState,
        timing: dict[str, float],
        vision: dict[str, Any] | None,
        board: Board,
    ) -> None:
        """Fold this committed move into the per-game reliability accumulators."""
        telem = state.telem
        counters = self._counters
        if counters.gate_hit or counters.gate_fallback:
            telem.gate_moves += 1
            if counters.gate_hit:
                telem.gate_hits += 1
        if counters.swipe_attempts > 1:
            telem.swipe_retry_total += counters.swipe_attempts - 1
        if counters.capture_screencap:
            telem.capture_screencap_moves += 1
        if vision is not None and vision["suspect"] > 0:
            telem.vision_fallback_moves += 1
        telem.move_ms.append(timing["total_ms"])
        top = engine.max_exponent(board)
        if telem.first_1024_ply is None and top >= 10:
            telem.first_1024_ply = state.ply
        if telem.first_2048_ply is None and top >= 11:
            telem.first_2048_ply = state.ply

    # -- small helpers --------------------------------------------------------------

    def _debug_snap(self, reason: str, state: _GameState, **context: Any) -> None:
        """Fire-and-forget on-error debug bundle (screenshot + APK + boards).

        Delegates to the injected :class:`~bot2048.runner.debug_capture.DebugCapture`
        (``None`` / disabled = no-op). The capture runs OFF this thread and
        never raises -- a debug artifact must never break or slow the move.
        """
        if self.debug_capture is None:
            return
        self.debug_capture.capture(
            reason=reason,
            game_id=state.game_id,
            ply=state.ply,
            context=context,
        )

    def _settle_sleep(self, seconds: float) -> None:
        """A deliberate settle/poll/retry wait, billed to the SETTLE phase."""
        with self._timer.phase(_SETTLE):
            self._sleep(seconds)

"""Perception gate for the live loop (R4, docs/archive/refactor-plan.md).

The fail-closed heart of the live pipeline: every board the loop acts on comes
from here. :class:`ObservationGate` owns capture, stable-frame settle, the
predict-gate and the fast APK packet gate; :class:`~bot2048.runner.live.LiveRunner`
holds one and delegates to it. The extraction is behavior-preserving:

* the terminal-popup exceptions (:class:`~bot2048.runner.outcomes._WinPopup2048`
  / :class:`~bot2048.runner.outcomes._GameOverPopup`) are raised from
  :meth:`capture_game_frame` exactly as before -- the loop
  (``_acquire_initial_game`` / ``_play_game``) still catches them;
* the AUTH_REQUIRED branch is handed back to the loop's own auth flow via the
  injected ``auth_handler`` (``LiveRunner._handle_auth``), which may itself
  raise :class:`~bot2048.runner.outcomes._GameReset` / a popup exception / FATAL
  -- unchanged;
* ``last_frame`` (the frame behind the latest parse) lives here now and is read
  by the loop through :attr:`last_frame` (auto-learn / vision snapshot / the
  postmortem screenshot);
* the shared :class:`~bot2048.runner.move_telemetry._MovePhaseTimer` /
  :class:`~bot2048.runner.move_telemetry._MoveCounters` are the loop's own
  instances, so the ``timing`` / ``move`` eval_details buckets stay byte-identical.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

import structlog

from bot2048.core.types import Board
from bot2048.runner.alerts import AlertCode
from bot2048.runner.errors import LiveLoopError
from bot2048.runner.escalation import ProbeResult, probe_screen
from bot2048.runner.incidents import Image, MoveRing
from bot2048.runner.live_config import (
    _APK_LULL_POLL_S,
    _GATE_SPAWN_EXPONENTS,
    LiveConfig,
)
from bot2048.runner.move_telemetry import (
    _PERCEIVE,
    _SETTLE,
    _MoveCounters,
    _MovePhaseTimer,
    _TimedDetector,
    _TimedDevice,
)
from bot2048.runner.outcomes import _GameOverPopup, _WinPopup2048, diff_spawn
from bot2048.storage.schema import board_to_hex
from bot2048.vision.states import ScreenState

if TYPE_CHECKING:
    from bot2048.runner.live import BoardReader, BoardSource, GameSupervisor, _GameState

log = structlog.get_logger(__name__)


class ObservationGate:
    """Capture / settle / predict-gate / APK-observe for one live session.

    All collaborators are injected by :class:`~bot2048.runner.live.LiveRunner`.
    ``clock`` / ``sleep`` are passed as thin accessors of the runner's current
    attributes (not bound once) so a test that swaps ``runner._clock`` /
    ``runner._sleep`` after construction is still honored -- the exact
    pre-extraction behavior of ``self._clock`` / ``self._sleep``.
    """

    def __init__(
        self,
        *,
        timed_device: _TimedDevice,
        timed_detect: _TimedDetector,
        read_board: BoardReader,
        board_source: BoardSource | None,
        supervisor: GameSupervisor,
        config: LiveConfig,
        timer: _MovePhaseTimer,
        counters: _MoveCounters,
        capture_lock: AbstractContextManager[bool],
        clock: Callable[[], float],
        sleep: Callable[[float], Any],
        auth_handler: Callable[[_GameState | None, ProbeResult], Image],
        notify: Callable[[str, str, str], None],
        ring: MoveRing | None = None,
    ) -> None:
        self._timed_device = timed_device
        self._timed_detect = timed_detect
        self.read_board = read_board
        self.board_source = board_source
        self.supervisor = supervisor
        self.config = config
        self._timer = timer
        self._counters = counters
        self._capture_lock = capture_lock
        self._clock = clock
        self._sleep = sleep
        self._auth_handler = auth_handler
        self._notify = notify
        self.ring = ring
        self._last_frame: Image | None = None  # frame behind the latest parse

    @property
    def last_frame(self) -> Image | None:
        """The frame behind the latest parse (auto-learn / postmortem / vision)."""
        return self._last_frame

    # -- verification (the fast APK gate first, then the frame path) --------------

    def observe_after_swipe(
        self, state: _GameState, predicted: Board
    ) -> tuple[str, Board]:
        """Verify the swipe: fast APK packet gate first, then the frame path.

        When an APK :class:`BoardSource` is injected, :meth:`_observe_via_apk`
        gets first crack -- it accepts the first PACKET whose board is
        ``predicted`` plus a settled 2/4 spawn (~one socket read, no vision).
        It returns ``None`` (defers to the full-frame path) whenever the fast
        answer is ambiguous or the link is silent/dropped: a non-matching
        board, a not-in-game / secure-suspected packet (confirmed by a full
        frame there), or a lull. The frame path
        (:meth:`_observe_after_swipe_frames`) is then the exact pre-APK
        verification -- so the packet gate can only speed up the happy path,
        never weaken the fail-closed guarantees.
        """
        if self.board_source is not None:
            gated = self._observe_via_apk(state, predicted)
            if gated is not None:
                return gated
        return self._observe_after_swipe_frames(state, predicted)

    def _observe_after_swipe_frames(
        self, state: _GameState, predicted: Board
    ) -> tuple[str, Board]:
        """Re-capture until the board verifies: predict-gate, then settle.

        Returns ``("spawn" | "unchanged" | "mismatch", last_parsed_board)``.
        The predict-gate (:meth:`_predict_gate`) accepts the first frame that
        IS ``predicted`` plus one settled 2/4 spawn and returns early. When
        it does not converge (rejected swipe, unknown merged tile, corrupted
        parse), the legacy path below runs UNCHANGED: every candidate board
        comes from :meth:`settled_board`, so a mid-merge animation frame is
        never classified as a diff.
        """
        gated = self._predict_gate(state, predicted)
        if gated is not None:
            return ("spawn", gated)
        parsed = state.board
        for i in range(self.config.parse_retries):
            parsed = self.settled_board(state)
            spawn = diff_spawn(predicted, parsed)
            # Gate the legacy accept path on a 2/4 spawn exactly like the
            # predict-gate / APK-gate paths above: a settled but MISREAD tile
            # (any exponent that is not a legal spawn) must NOT be accepted as
            # the move's spawn, or a corrupt board would commit to the fixed
            # policy and burn a deep game. A non-2/4 diff falls through to the
            # unchanged/mismatch classification, where the mismatch recovery
            # (auto-learn / occlusion / VISION_MISMATCH abort) owns it.
            if spawn is not None and spawn.exponent in _GATE_SPAWN_EXPONENTS:
                return ("spawn", parsed)
            if i + 1 < self.config.parse_retries:
                self._settle_sleep(self.config.settle_delay_s)  # animation may still run
        return ("unchanged" if parsed == state.board else "mismatch", parsed)

    def _predict_gate(self, state: _GameState, predicted: Board) -> Board | None:
        """Early-exit verification: first frame == prediction + settled 2/4.

        The engine is deterministic and knows the full pre-spawn board
        (``predicted``), so ONE frame is enough to verify a move: accept the
        FIRST capture whose non-spawn part equals ``predicted`` AND whose
        only new tile is a settled 2/4 on a predicted-empty cell
        (:func:`diff_spawn` + :data:`_GATE_SPAWN_EXPONENTS`). A transient
        slide frame cannot satisfy the full-board equality (some cell always
        still differs), so a false early frame is simply skipped and the
        next capture is polled (docs/research/early_spawn_detection.md).

        Returns the accepted board, or ``None`` once
        :attr:`LiveConfig.predict_gate_max_wait_s` is exhausted so the
        caller falls back to the legacy stable-frame settle (zero
        reliability regression: the gate never classifies, only accepts).
        """
        cfg = self.config
        if not cfg.predict_gate:
            return None
        deadline = self._clock() + cfg.predict_gate_max_wait_s
        frames = 0
        while True:
            parsed = self.parse_or_escalate(self.capture_game_frame(state), state=state)
            frames += 1
            spawn = diff_spawn(predicted, parsed)
            if spawn is not None and spawn.exponent in _GATE_SPAWN_EXPONENTS:
                # Sealed into eval_details["move"] at commit: the early-spawn
                # metric #1, until now only in this debug line.
                self._counters.gate_hit = True
                self._counters.gate_frames += frames
                log.debug(
                    "live.predict_gate_hit",
                    frames=frames,
                    spawn_cell=spawn.cell,
                    spawn_exponent=spawn.exponent,
                    game_id=state.game_id,
                    ply=state.ply,
                )
                return parsed
            if self._clock() >= deadline:
                self._counters.gate_fallback = True
                self._counters.gate_frames += frames
                log.info(
                    "live.predict_gate_fallback",
                    frames=frames,
                    max_wait_s=cfg.predict_gate_max_wait_s,
                    board=board_to_hex(parsed),
                    game_id=state.game_id,
                    ply=state.ply,
                )
                return None
            self._settle_sleep(cfg.predict_gate_poll_s)

    def _observe_via_apk(
        self, state: _GameState, predicted: Board
    ) -> tuple[str, Board] | None:
        """Fast packet gate: accept the first packet == prediction + settled 2/4.

        Polls the injected :class:`BoardSource` for board packets until one is
        ``predicted`` plus exactly one settled 2/4 spawn on a predicted-empty
        cell (the same :func:`diff_spawn` + :data:`_GATE_SPAWN_EXPONENTS`
        acceptance the frame predict-gate uses, so a mid-slide packet can never
        be mistaken for the spawn). Returns ``("spawn", board)`` on a hit.

        Returns ``None`` -- defer to the full-frame verification -- in every
        ambiguous case, so the fast path never guesses:

        * a not-in-game / secure-suspected packet: the board is off-screen
          (popup / auth wall). A full frame is pulled RIGHT HERE via
          :meth:`capture_game_frame` (fail-closed) so a game-over/win popup or
          the auth wall is surfaced immediately; a healthy return means it was
          transient and the frame path re-verifies.
        * the link is silent or dropped (``poll`` -> ``None``): no fast board.
        * the budget (:attr:`LiveConfig.apk_gate_max_wait_s`) elapses without a
          matching packet (rejected swipe, unknown merged tile): the frame path
          then classifies unchanged vs mismatch with full reliability.
        """
        source = self.board_source
        assert source is not None  # caller guards
        deadline = self._clock() + self.config.apk_gate_max_wait_s
        while True:
            with self._timer.phase(_PERCEIVE):
                sample = source.poll()
            if sample is None:
                # A momentary packet lull (a poll read-timeout / brief WiFi
                # stall) is NOT worth a ~1.3 s full-frame screencap: right after
                # a swipe the settled spawn packet is usually just late. Stay on
                # the cheap packet path until the SAME gate budget elapses, then
                # fall through to the frame path -- fail-closed is intact: a real
                # popup/auth wall arrives as a not_in_game packet (handled just
                # below), and a persistently silent/dropped link still ends at
                # the full-frame verify once the budget runs out. Previously this
                # bailed on the FIRST empty poll, so every transient lull paid a
                # foreground screencap on the critical path (the capture tail).
                if self._clock() >= deadline:
                    return None  # silent / dropped link -> screencap full frame
                self._settle_sleep(self.config.apk_poll_s or _APK_LULL_POLL_S)
                continue
            if not sample.trustworthy:
                # The packet says the board is not on screen: CONFIRM with a
                # full frame (raises the popup/auth path). If it comes back
                # healthy the flag was transient -> fall back to the frame path.
                log.info(
                    "live.apk_not_in_game",
                    seq=sample.seq,
                    not_in_game=sample.not_in_game,
                    secure=sample.secure_suspected,
                    game_id=state.game_id,
                    ply=state.ply,
                )
                self.capture_game_frame(state)
                return None
            spawn = diff_spawn(predicted, sample.board)
            if spawn is not None and spawn.exponent in _GATE_SPAWN_EXPONENTS:
                # Packet-age telemetry: how long the ACCEPTED packet sat in the
                # source's mailbox before this poll consumed it. Sealed into
                # eval_details["move"]["apk_age_ms"] at commit -- the direct
                # measure of the background-drain latency win, surfaced by
                # ``bot2048 db latency``. None (a bare test double) stays None.
                self._counters.apk_age_ms = sample.age_ms
                log.debug(
                    "live.apk_gate_hit",
                    seq=sample.seq,
                    spawn_cell=spawn.cell,
                    spawn_exponent=spawn.exponent,
                    age_ms=sample.age_ms,
                    game_id=state.game_id,
                    ply=state.ply,
                )
                return ("spawn", sample.board)
            if self._clock() >= deadline:
                log.info(
                    "live.apk_gate_fallback",
                    seq=sample.seq,
                    board=board_to_hex(sample.board),
                    game_id=state.game_id,
                    ply=state.ply,
                )
                return None
            if self.config.apk_poll_s:
                self._settle_sleep(self.config.apk_poll_s)

    # -- screen handling ----------------------------------------------------------

    def settled_board(
        self, state: _GameState | None, frame: Image | None = None
    ) -> Board:
        """Parse a frame that has stopped animating (wait-for-stable-frame).

        Captures (starting from ``frame`` when given) until
        :attr:`LiveConfig.settle_stable_frames` consecutive captures parse to
        the same board, sleeping :attr:`LiveConfig.settle_poll_s` between
        them. Gives up after :attr:`LiveConfig.settle_max_wait_s` and returns
        the LAST parse -- a genuinely inconsistent screen still reaches the
        caller's unchanged/mismatch classification unchanged; only transient
        animation frames are filtered out.
        """
        if frame is None:
            frame = self.capture_game_frame(state)
        board = self.parse_or_escalate(frame, state=state)
        needed = self.config.settle_stable_frames
        if needed <= 1:
            return board
        stable = 1
        deadline = self._clock() + self.config.settle_max_wait_s
        while stable < needed:
            if self._clock() >= deadline:
                log.warning(
                    "live.settle_timeout",
                    stable=stable,
                    needed=needed,
                    max_wait_s=self.config.settle_max_wait_s,
                    board=board_to_hex(board),
                )
                return board
            self._settle_sleep(self.config.settle_poll_s)
            nxt = self.parse_or_escalate(self.capture_game_frame(state), state=state)
            stable = stable + 1 if nxt == board else 1
            board = nxt
        return board

    def capture_game_frame(self, state: _GameState | None) -> Image:
        """A frame that classifies as healthy GAME, or escalate trying."""
        # The timed delegates bill screenshot/classification to the CAPTURE /
        # DETECT phases of the current move; probe_screen itself is unchanged.
        # The lock serializes this full-frame screencap against the async
        # background worker's own screencap on the shared adb channel.
        with self._capture_lock:
            probe = probe_screen(self._timed_device, self._timed_detect)
        if probe.state is ScreenState.AUTH_REQUIRED:
            return self._auth_handler(state, probe)
        if probe.state is ScreenState.WIN_POPUP_2048:
            # The app declared the game WON -- a terminal state, not an
            # incident: never send it through the supervisor error path.
            raise _WinPopup2048(probe.reason)
        if probe.state is ScreenState.GAME_OVER_POPUP:
            # The app declared the game OVER (board filled) -- a natural
            # terminal state, not an incident: finalize like an engine-decided
            # game over, never through the supervisor error path.
            raise _GameOverPopup(probe.reason)
        if probe.healthy:
            assert probe.image is not None
            if self.ring is not None:
                self.ring.set_good_frame(probe.image)
            return probe.image
        # CONFIRM_NEW_GAME / SCREEN_OFF recover in one supervisor cycle;
        # NOT_IN_GAME / UNKNOWN / DEVICE_LOST become FATAL there (§5).
        image = self.supervisor.handle_error(
            AlertCode.SCREEN_UNEXPECTED,
            f"expected GAME, got {probe.state.value}: {probe.reason}",
            probe=probe,
            game_id=state.game_id if state is not None else None,
            move_no=state.ply if state is not None else None,
        )
        self._notify("INFO", "RECOVERED", f"✅ Відновлено ({probe.state.value} → GAME)")
        if self.ring is not None:
            self.ring.set_good_frame(image)
        return image

    def parse_or_escalate(self, frame: Image, state: _GameState | None = None) -> Board:
        self._last_frame = frame  # the frame behind the latest parse (auto-learn)
        try:
            with self._timer.phase(_PERCEIVE):
                return self.read_board(frame)
        except ValueError as exc:  # vision BoardParseError and kin
            image = self.supervisor.handle_error(
                AlertCode.VISION_MISMATCH,
                f"board parse failed: {exc}",
                game_id=state.game_id if state is not None else None,
                move_no=state.ply if state is not None else None,
            )
            try:
                with self._timer.phase(_PERCEIVE):
                    return self.read_board(image)
            except ValueError as exc2:
                raise LiveLoopError(
                    f"board unparseable even after supervisor recovery: {exc2}"
                ) from exc2

    # -- small helpers --------------------------------------------------------------

    def _settle_sleep(self, seconds: float) -> None:
        """A deliberate settle/poll/retry wait, billed to the SETTLE phase."""
        with self._timer.phase(_SETTLE):
            self._sleep(seconds)

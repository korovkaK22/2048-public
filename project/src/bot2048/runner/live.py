"""Live game loop: play 2048 inside the monobank app over ADB.

Per iteration (docs/architecture.md, docs/ops-monitoring.md):

1.  Pause gate -- while ``control/pause.flag`` exists the loop parks between
    moves (the heartbeat thread keeps beating, so a pause never trips
    WATCHDOG_TIMEOUT). Entering sends "⏸ Пауза — можеш взяти телефон",
    leaving sends "🚫 Продовжую — не чіпай телефон" (via the notify hook).
2.  ``probe_screen`` -> :class:`~bot2048.vision.states.ScreenState` branch:

    * ``GAME`` -> play (steps 3-7);
    * ``AUTH_REQUIRED`` -> **auth pause**: all input stops immediately.
      Auto-login is opt-in (:attr:`LiveConfig.auto_login` + ``MONO_PIN``);
      without it the supervisor escalates AUTH_REQUIRED as FATAL (banking
      app: zero taps, a human unlocks). After a successful auto-login the
      parsed board decides resume-vs-reset (docs/edge-cases.md): identical
      board -> resume; anything else -> the app was killed and regenerated
      the game -> the old game closes as ``end_reason='interrupted'`` and a
      brand-new game is recorded from full re-recognition;
    * ``CONFIRM_NEW_GAME`` / ``SCREEN_OFF`` -> one supervisor recovery cycle
      (CONTINUE_GAME tap / KEYCODE_WAKEUP);
    * ``NOT_IN_GAME`` / ``UNKNOWN`` / ``DEVICE_LOST`` -> supervisor -> FATAL.
3.  ``read_board`` -> ``Board`` (vision is injected behind
    :class:`BoardReader` / :class:`StateDetector`, so this module never
    imports the concrete parser -- vision/ is being rewritten concurrently).
4.  Game over is decided by ``core.engine.is_game_over`` on the parsed
    board -- NEVER by the popup (known app bug: the clone may keep running
    when no moves remain).
5.  ``strategy.choose`` -> :class:`~bot2048.ai.strategies.Decision`; the
    engine predicts the after-move board.
6.  Humanized swipe, then VERIFY: the parsed board must equal the
    prediction plus exactly one new tile (:func:`diff_spawn`).
    Verification is two-tier (docs/research/early_spawn_detection.md):

    * **predict-gate** (:meth:`LiveRunner._predict_gate`, on by default via
      :attr:`LiveConfig.predict_gate`): the deterministic engine knows the
      exact pre-spawn board, so post-swipe frames are polled and the FIRST
      frame that equals the prediction plus exactly one settled 2/4 tile on
      a predicted-empty cell is accepted immediately. A transient slide
      frame can never satisfy the full-board equality (some cell always
      still differs), so the hot path needs no stable-frame wait.
    * **legacy settle fallback**: when the gate does not converge within
      :attr:`LiveConfig.predict_gate_max_wait_s` (rejected swipe, unknown
      merged tile, corrupted parse), every candidate board comes from a
      **stable frame** (:meth:`LiveRunner._settled_board`): captures repeat
      until :attr:`LiveConfig.settle_stable_frames` consecutive parses
      agree (polling every :attr:`LiveConfig.settle_poll_s`, bounded by
      :attr:`LiveConfig.settle_max_wait_s`), so a mid-merge animation frame
      is never classified as a diff -- zero reliability regression.

    Unchanged (settled) board -> the swipe did
    not register: retry it up to
    :attr:`LiveConfig.swipe_retries` times, then a MOVE_REJECTED ERROR alert
    and the game is aborted (``end_reason='move_rejected'``). Any other diff ->
    re-capture up to :attr:`LiveConfig.parse_retries` times (animation may
    still be running), then VISION_MISMATCH + abort
    (``end_reason='vision_mismatch'``). The DB never receives a move that was
    not verified on screen.
7.  Record ``MovePlayed`` + ``TileSpawned`` (the verification diff IS the
    spawn) and ``GameEnded`` -- every live game is fully reconstructable
    from its events alone, same as simulated ones.

A game killed mid-play (FATAL incident, deliberate stop, any crash) is still
finalized before the exception escalates: :meth:`LiveRunner.run` emits
``GameEnded`` from the last verified board. The ``end_reason`` names the actual
cause (:func:`_abort_reason`) -- ``'auth_required'`` / ``'vision_mismatch'`` /
``'device_lost'`` ... for an operational incident, ``'interrupted'`` for a
deliberate KeyboardInterrupt, and ``'error'`` only for a genuine internal crash
-- so the summary row never stays half-written (NULL score/moves) nor blames a
clean operational abort on a code bug, and the original exception then
propagates unchanged.

Timer semantics (docs/device-ui-notes.md): the in-game timer is pure
wall-clock from the confirm tap; ``Supervisor.start_new_game`` returns that
``t0`` and ``GameEnded.duration_s`` is measured against it.

Config note: ``device/config.py`` has no ``LiveConfig`` (its ``DeviceConfig``
only covers the adb transport), so the loop parameters live in the
runner-local :class:`LiveConfig` below.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn, Protocol

import structlog

from bot2048.ai.strategies import Strategy
from bot2048.core import engine
from bot2048.core.types import Board, Direction, Spawn

# NOTE: module import, not a from-import: device/supervisor.py itself imports
# bot2048.runner.incidents, so binding FatalIncident at import time crashes on
# the half-initialized module whenever the device layer happens to load first.
from bot2048.device import supervisor as _supervisor_mod
from bot2048.device.adb import DeviceError
from bot2048.device.apk_capture import BoardSample
from bot2048.device.auth import read_pin
from bot2048.device.humanizer import BBox, Humanizer, HumanizerConfig, SwipeParams
from bot2048.recording import (
    GameEnded,
    GameEvent,
    GameStarted,
    Recorder,
    TileSpawned,
)
from bot2048.runner import incidents
from bot2048.runner.alerts import Alert, AlertCode, AlertLevel, AlertWriter
from bot2048.runner.auth_flow import AuthFlow
from bot2048.runner.bg_detect import BackgroundStateProbe, StateProbe
from bot2048.runner.budget_ladder import BudgetLadder
from bot2048.runner.debug_capture import DEBUG_CAPTURE_REASONS, DebugCapture
from bot2048.runner.errors import LiveLoopError
from bot2048.runner.escalation import ProbeResult, policy_for, probe_screen

# Game-end notification formatting lives in game_end_notify.py; the names are
# re-exported here (``__all__`` / the ``noqa`` re-exports) because tests and
# downstream callers import them from this module.
from bot2048.runner.game_end_notify import (  # noqa: F401  (re-exports)
    FARM_SIM_MEAN,
    _format_game_end_notify,
    _render_final_grid,
    format_duration,
)
from bot2048.runner.incidents import Image, MoveRing
from bot2048.runner.live_config import (
    DEFAULT_DEBUG_CAPTURES_ROOT,
    DEFAULT_GAME_SCREENS_ROOT,
    SETTLE_DELAY_DEFAULT_S,
    LiveConfig,
)
from bot2048.runner.move_telemetry import (
    _board_stats,
    _GameTelemetry,
    _MoveCounters,
    _MovePhaseTimer,
    _percentile,
    _TimedDetector,
    _TimedDevice,
)
from bot2048.runner.newgame import (
    FRESH_MAX_START_EXPONENT,
    FRESH_MAX_START_SUM,
    FRESH_STARTER_TILES,
    NewGameError,
    _fresh_board_reason,
    reset_to_new_game,
)
from bot2048.runner.observation import ObservationGate
from bot2048.runner.outcomes import (
    HELD_BEFORE_WIN_REASON,
    WIN_EXPONENT,
    _abort_reason,
    _GameOverPopup,
    _GameReset,
    _HeldBeforeWin,
    _WinPopup2048,
    diff_spawn,
)
from bot2048.runner.session_recovery import SessionRecovery
from bot2048.runner.swipe_controller import SwipeController
from bot2048.storage.errors import StorageError
from bot2048.storage.history import FarmPercentile, farm_score_percentile
from bot2048.storage.ops_schema import OpsRepository
from bot2048.storage.schema import board_to_hex
from bot2048.tgnotify import PAUSE_FLAG_PATH
from bot2048.vision.states import ScreenState, StateDetection

log = structlog.get_logger(__name__)

#: How fresh the newest APK board packet must be (seconds) for the background
#: probe to treat the frame stream as "flowing" (device alive). The sampler's
#: unchanged-board heartbeat ceiling is 2.55 s (``encode_set_rate``: one byte
#: in 10 ms units), so 5 s spans ~2 missed heartbeats before the stream is
#: considered stopped -- late enough to avoid flapping, early enough that a
#: genuinely dead link still reaches DEVICE_LOST within one probe interval.
APK_FRAMES_FRESH_WINDOW_S: float = 5.0

__all__ = [
    "FARM_SIM_MEAN",
    "SETTLE_DELAY_DEFAULT_S",
    "BoardReader",
    "BoardSource",
    "CellLook",
    "CellsReader",
    "GameSupervisor",
    "HeartbeatSink",
    "LearnTileHook",
    "LiveConfig",
    "LiveDevice",
    "LiveLoopError",
    "LiveRunner",
    "NotifyHook",
    "StateDetector",
    "diff_spawn",
    "format_duration",
]


# -- injected contracts --------------------------------------------------------
#
# The loop depends on vision/device/lifecycle only through these local
# Protocols; the concrete implementations are wired in runner/cli.py. Tests
# run the loop against in-memory fakes.


class StateDetector(Protocol):
    """``detect_state`` bound to calibration+anchors (vision impl injected)."""

    def __call__(self, image: Image) -> StateDetection: ...


class BoardReader(Protocol):
    """``parse_board`` bound to a calibration (vision impl injected)."""

    def __call__(self, image: Image) -> Board: ...


class BoardSource(Protocol):
    """OPTIONAL fast on-device board perception (the sampler APK, option D).

    When injected, the loop reads the per-move board from 22-byte packets
    (:class:`~bot2048.device.apk_capture.BoardSample`) instead of parsing a
    full frame -- ``detect_state`` still runs on periodic/on-suspicion full
    frames (the packet cannot see popups/auth). ``poll`` returns the next
    sample or ``None`` when the fast path has nothing (lull or dropped link),
    which the loop treats as "fall back to the screencap full frame", never as
    a board. :class:`~bot2048.device.apk_capture.ApkBoardSource` satisfies it.
    """

    def start(self) -> None: ...

    def poll(self) -> BoardSample | None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CellLook:
    """Runner-side view of one parsed cell (trust info for auto-learn).

    ``suspect`` is True when vision decided the cell WITHOUT a clean digit
    read (color fallback / low confidence / otherwise untrusted) -- exactly
    the reads the engine prediction may overrule for a freshly merged tile
    the recognizer does not know yet.
    """

    exponent: int
    suspect: bool


class CellsReader(Protocol):
    """Per-cell recognition detail of a frame (``parse_board_detailed`` bound).

    Returns ``None`` when no detail is available (e.g. the detailed parse
    raised); the caller then keeps the plain VISION_MISMATCH abort.
    """

    def __call__(self, image: Image) -> tuple[CellLook, ...] | None: ...


LearnTileHook = Callable[[Image, int, int], bool]
"""``(frame, cell, exponent) -> learned?`` -- wraps ``vision.learn_tile_color``,
swaps the in-memory calibration and persists it; ``False`` = sample refused
(unsettled frame / too close to another tile's color), do NOT trust it."""


class LiveDevice(Protocol):
    """Device surface the loop needs; ``AdbDevice`` satisfies it."""

    def screenshot(self) -> Image: ...
    def ui_dump(self) -> str: ...
    def tap(self, x: int, y: int, *, sensitive: bool = False) -> None: ...
    def wake(self) -> None: ...
    def swipe_direction(
        self, direction: Direction, grid_bbox: BBox, humanizer: Humanizer
    ) -> SwipeParams: ...


class GameSupervisor(Protocol):
    """The ERROR/FATAL surface of :class:`bot2048.device.supervisor.Supervisor`."""

    #: Set by the runner so the restart flow can delegate an auth wall (expired
    #: token) back to the runner's opt-in auto-login (see ``AuthFlow.resolve_restart_auth``).
    auth_resolver: _supervisor_mod.AuthResolver | None

    def handle_error(
        self,
        code: AlertCode,
        message: str,
        *,
        probe: ProbeResult | None = ...,
        game_id: str | None = ...,
        move_no: int | None = ...,
        board_hex: str | None = ...,
        attempt: int | None = ...,
        extra: dict[str, Any] | None = ...,
        stage: str | None = ...,
        attempts: int | None = ...,
    ) -> Image: ...

    def start_new_game(self, *, after_win: bool = ...) -> tuple[float, Image]: ...

    def fatal(
        self,
        code: AlertCode,
        message: str,
        *,
        probe: ProbeResult | None = ...,
        game_id: str | None = ...,
        move_no: int | None = ...,
        stage: str | None = ...,
        attempts: int | None = ...,
    ) -> NoReturn: ...


class HeartbeatSink(Protocol):
    """``HeartbeatWriter.update`` -- the only heartbeat call the loop makes."""

    def update(self, *, game_id: str | None = ..., ply: int | None = ...) -> None: ...


class NotifyHook(Protocol):
    """``(level, code, text[, photo_path])`` fire-and-forget hook.

    :meth:`bot2048.tgnotify.TgNotifier.notify` satisfies it. The optional
    ``photo_path`` attaches an image to a notification (the rendered final board
    / the last game frame at game over); omit it for a plain text alert.
    """

    def __call__(
        self, level: str, code: str, text: str, photo_path: str | None = ...
    ) -> None: ...

PinReader = Callable[[], str | None]
"""Returns the auto-login PIN or ``None`` (default: ``device.auth.read_pin``)."""


@dataclass(slots=True)
class _GameState:
    """Mutable per-game bookkeeping."""

    game_id: str
    board: Board
    t0: float  # monotonic zero of the in-game wall-clock timer
    score_total: int = 0
    ply: int = 0
    ended: bool = False  # GameEnded emitted (guards abort finalization)
    #: Mid-game budget escalation stepped at least once for THIS game (any
    #: ladder tier above T0; see :meth:`BudgetLadder.maybe_escalate`).
    budget_escalated: bool = False
    #: Highest budget-ladder tier reached by THIS game (0 = T0 base bulk,
    #: 1 = T1 late-game, 2 = T2 endgame). Monotonic: the ladder only steps
    #: UP within a game (a merge freeing empty cells never drops the budget
    #: back), and a new ``_GameState`` resets it to T0 -- games stay
    #: independent record attempts.
    budget_tier: int = 0
    telem: _GameTelemetry = field(default_factory=_GameTelemetry)


class LiveRunner:
    """Wires device + vision + strategy + recorder + supervisor into the loop.

    Everything with side effects (device, vision, supervisor, sleep, clock,
    notifications) is injected, so the full loop runs in unit tests against
    an in-memory fake phone with zero real delays.
    """

    def __init__(
        self,
        *,
        device: LiveDevice,
        detect_state: StateDetector,
        read_board: BoardReader,
        board_source: BoardSource | None = None,
        strategy: Strategy,
        recorder: Recorder,
        supervisor: GameSupervisor,
        alerts: AlertWriter,
        grid_bbox: BBox,
        humanizer: Humanizer | None = None,
        config: LiveConfig | None = None,
        run_id: str | None = None,
        ops: OpsRepository | None = None,
        heartbeat: HeartbeatSink | None = None,
        ring: MoveRing | None = None,
        notify: NotifyHook | None = None,
        pause_flag: Path = PAUSE_FLAG_PATH,
        pin_reader: PinReader = read_pin,
        read_cells: CellsReader | None = None,
        learn_tile: LearnTileHook | None = None,
        reconnect: Callable[[], Any] | None = None,
        bg_detect: StateProbe | None = None,
        incidents_root: Path = incidents.DEFAULT_INCIDENTS_ROOT,
        screens_root: Path = DEFAULT_GAME_SCREENS_ROOT,
        debug_root: Path = DEFAULT_DEBUG_CAPTURES_ROOT,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device = device
        self.detect = detect_state
        self.read_board = read_board
        self.board_source = board_source
        self.read_cells = read_cells
        self.learn_tile = learn_tile
        self.reconnect = reconnect
        self.incidents_root = Path(incidents_root)
        self.screens_root = Path(screens_root)
        self.strategy = strategy
        self.recorder = recorder
        self.supervisor = supervisor
        self.alerts = alerts
        self.grid_bbox = grid_bbox
        self.config = config or LiveConfig()
        self.humanizer = humanizer or self._build_humanizer(self.config)
        self.run_id = run_id
        self.ops = ops
        self.heartbeat = heartbeat
        self.ring = ring
        self.notify = notify
        self.pause_flag = Path(pause_flag)
        self._sleep = sleep
        self._clock = clock
        # Per-move latency telemetry (docs/research/latency_telemetry.md): capture
        # and detect_state are billed through thin delegates so probe_screen
        # stays untouched; perceive/swipe/settle are billed at the call sites.
        self._timer = _MovePhaseTimer(clock)
        # Per-move reliability counters (docs/research/telemetry_review.md M1-M3):
        # capture/swipe tallies, predict-gate outcome and the FACTUAL frame
        # source, sealed into eval_details["move"] beside the phase timing.
        self._counters = _MoveCounters()
        self._timed_device = _TimedDevice(device, self._timer, self._counters)
        self._timed_detect = _TimedDetector(detect_state, self._timer)
        # Start-of-session «Нова гра» seed (#46): when reset_on_start succeeds,
        # the verified fresh board + its zeroed t0 are stashed here and consumed
        # by the FIRST _acquire_initial_game instead of probing the live screen.
        self._initial_seed: tuple[Board, float] | None = None
        # Low-score churn guard: consecutive games that ended after too few
        # committed moves (see LiveConfig.low_score_churn_*). A healthy-length
        # game resets it; a run of short games raises an ERROR alert.
        self._low_score_churn_streak: int = 0
        # Async control-tick worker (APK mode): the periodic full-frame
        # detect_state runs OFF the play loop so the ~0.65 s WiFi screencap
        # never freezes the board. Built lazily in _start_bg_detect when an
        # APK source is wired and the cadence is enabled; an injected probe
        # (tests) is used verbatim. The lock serializes the worker's screencap
        # with the loop's own full-frame captures (shared adb channel).
        self._bg_detect: StateProbe | None = bg_detect
        self._capture_lock = threading.Lock()
        # THE incident path (docs/ops-monitoring.md §4). Shared with the
        # supervisor when it exposes its sink (one chain/dedupe state, one
        # prune budget for the whole session); a bare fake supervisor gets a
        # runner-owned sink. The ring carries the FRESH-capture hook so every
        # sink in the process -- this one, the supervisor's, the lifecycle
        # CRASH handler's -- can screenshot the phone at incident time.
        shared_sink = getattr(supervisor, "incidents", None)
        self._incidents: incidents.IncidentSink = (
            shared_sink
            if isinstance(shared_sink, incidents.IncidentSink)
            else incidents.IncidentSink(
                root=self.incidents_root,
                alerts=self.alerts,
                ops=self.ops,
                run_id=self.run_id,
                ring=self.ring,
            )
        )
        self._incidents.context_provider = self._incident_context
        # A supervisor wired without ops/run_id/ring (tests, bare wiring) still
        # gets the runner's: the DB mirror and the fresh-capture hook live there.
        if self._incidents.ops is None:
            self._incidents.ops = self.ops
        if self._incidents.run_id is None:
            self._incidents.run_id = self.run_id
        if self.ring is not None:
            if self._incidents.ring is None:
                self._incidents.ring = self.ring
            self.ring.set_capture(self._incident_capture)
        # Auth-wall flow (R5): opt-in auto-login for the main loop
        # (``handle_auth``) and the restart flow (``resolve_restart_auth``). The
        # PIN-entry security contract is enforced entirely inside this module.
        # ``clock`` / ``sleep`` are accessors of the runner's CURRENT attributes
        # (not bound once), so a test swapping ``self._clock`` / ``self._sleep``
        # is honored. ``parse_or_escalate`` and ``on_game_interrupted`` are
        # handed back so the gate's board parse and the game finalization
        # (``_end_game(state, "interrupted")``) stay in this runner -- no
        # recording/gate logic leaks into AuthFlow. Built BEFORE the gate: the
        # gate wires ``handle_auth`` as its ``auth_handler``.
        self._auth = AuthFlow(
            device=self.device,
            detect=self.detect,
            config=self.config,
            pin_reader=pin_reader,
            supervisor=self.supervisor,
            capture_lock=self._capture_lock,
            clock=lambda: self._clock(),
            sleep=lambda seconds: self._sleep(seconds),
            parse_or_escalate=lambda frame, state: self._parse_or_escalate(frame, state=state),
            on_game_interrupted=lambda state: self._end_game(state, "interrupted"),
            notify=self._notify,
            ops_info=self._ops_info,
            ring=self.ring,
        )
        # The restart flow (supervisor.start_new_game) delegates an auth wall
        # (expired token -> «Нова гра» routes through re-auth) back to the same
        # opt-in auto-login path the main loop uses, so a token expiry between
        # games is seamless instead of a wasted GAME_OVER_UNEXPECTED FATAL.
        supervisor.auth_resolver = self._auth.resolve_restart_auth
        # Never-crash outer-loop recovery (R6): which failures are transient and
        # the sliding-hour restart budget that keeps overnight runs alive. The
        # policy is enforced entirely inside this module; the run-loop only asks
        # ``recoverable`` / ``recover``.
        self._recovery = SessionRecovery(
            config=self.config,
            clock=lambda: self._clock(),
            sleep=lambda seconds: self._sleep(seconds),
            alerts=self.alerts,
            incidents=self._incidents,
            run_id=self.run_id,
            notify=self._notify,
            reconnect=self.reconnect,
            heartbeat=self.heartbeat,
            ring=self.ring,
        )
        # Perception gate (R4): capture / settle / predict-gate / APK-observe.
        # It shares THIS runner's phase timer + reliability counters (so the
        # eval_details "timing"/"move" buckets stay byte-identical) and the
        # capture lock (serialized with the bg_detect worker). ``clock`` /
        # ``sleep`` are passed as accessors of the runner's CURRENT attributes,
        # not bound once, so a test that swaps ``self._clock`` / ``self._sleep``
        # after construction is honored exactly as before the extraction. The
        # AUTH_REQUIRED branch is handed back to the runner's auth flow
        # (``AuthFlow.handle_auth``), which may raise ``_GameReset`` / a popup / FATAL.
        self._gate = ObservationGate(
            timed_device=self._timed_device,
            timed_detect=self._timed_detect,
            read_board=self.read_board,
            board_source=self.board_source,
            supervisor=self.supervisor,
            config=self.config,
            timer=self._timer,
            counters=self._counters,
            capture_lock=self._capture_lock,
            clock=lambda: self._clock(),
            sleep=lambda seconds: self._sleep(seconds),
            auth_handler=self._auth.handle_auth,
            notify=self._notify,
            ring=self.ring,
        )
        # The frame behind the latest parse (``screenshot_last_parsed.png`` in
        # every bundle) lives in the gate; hand the sink a live accessor.
        self._incidents.last_frame_provider = lambda: self._gate.last_frame
        # On-error debug bundles (screenshot + APK packet + boards): built here
        # so the SwipeController's occlusion/mismatch hooks and this runner's
        # game-abort finalization share ONE sink (same dir, same prune bound,
        # same seq). Raw ``device.screenshot`` (a one-off diagnostic, not
        # per-move latency telemetry -- mirrors _capture_end_frame) behind the
        # shared capture lock; the APK packet is snapshotted from the same
        # mailbox the perception path reads (instant, caller's thread).
        self._debug = DebugCapture(
            screenshot=lambda: self.device.screenshot(),
            # Duck-typed: the BoardSource protocol does not declare ``latest``
            # (only the concrete ApkBoardSource mailbox has it); a source
            # without one simply yields no APK section in the sidecar.
            apk_latest=getattr(self.board_source, "latest", None),
            root=Path(debug_root),
            keep_files=self.config.debug_capture_keep,
            capture_lock=self._capture_lock,
            enabled=self.config.debug_capture_on_error,
        )
        # One-move execution (R7): choose -> swipe -> verify -> commit + the
        # auto-learn (vision self-heal) and per-move/-game telemetry. Built LAST
        # (depends on the gate for ``observe_after_swipe`` / ``last_frame`` and on
        # ``_auth`` for ``note_move_committed``). It shares THIS runner's phase
        # timer + reliability counters (so the ``timing`` / ``move`` eval_details
        # buckets stay byte-identical) and reuses the runner's own hooks
        # (``_record`` / ``_notify`` / ``_strategy_label`` / ``_apk_control_tick``)
        # so recording, the APK control tick and screen orchestration stay here --
        # no loop/gate logic leaks into the controller. ``clock`` / ``sleep`` are
        # accessors of the runner's CURRENT attributes, not bound once, so a test
        # swapping ``self._clock`` / ``self._sleep`` is honored.
        self._swipe = SwipeController(
            strategy=self.strategy,
            device=self.device,
            grid_bbox=self.grid_bbox,
            humanizer=self.humanizer,
            gate=self._gate,
            config=self.config,
            supervisor=self.supervisor,
            timer=self._timer,
            counters=self._counters,
            board_source=self.board_source,
            read_cells=self.read_cells,
            learn_tile=self.learn_tile,
            heartbeat=self.heartbeat,
            ring=self.ring,
            record=self._record,
            notify=self._notify,
            strategy_label=self._strategy_label,
            control_tick=self._apk_control_tick,
            note_move_committed=self._auth.note_move_committed,
            clock=lambda: self._clock(),
            sleep=lambda seconds: self._sleep(seconds),
            debug_capture=self._debug,
        )
        # Mid-game budget ladder (brief §3): pure policy over the go strategy's
        # ``budget_ms`` -- stepped UP after each committed move
        # (``_maybe_escalate_budget``), restored to the T0 base when a new game
        # begins (``_begin_game``) so games stay independent attempts.
        self._ladder = BudgetLadder(strategy=self.strategy, config=self.config)

    @property
    def _recoveries(self) -> deque[float]:
        """Session-recovery timestamps (owned by :class:`SessionRecovery`)."""
        return self._recovery.recoveries

    @property
    def _pre_escalation_budget_ms(self) -> float | None:
        """The remembered T0 base budget (owned by :class:`BudgetLadder`)."""
        return self._ladder.pre_escalation_budget_ms

    @staticmethod
    def _build_humanizer(config: LiveConfig) -> Humanizer:
        """The default humanizer, honoring :attr:`LiveConfig.swipe_duration_ms`.

        With no override this is a bare :class:`Humanizer` (profile from the
        ``BOT2048_PACING_PROFILE`` env, fast/80 ms default) -- behavior is
        unchanged. When ``swipe_duration_ms`` is set the env profile is kept but
        a FIXED (jitter-free) gesture duration is overlaid, so ``--swipe-duration``
        can A/B swipe speed vs the move_rejected retry rate without touching
        pacing. Zeroing the jitter also preserves the humanizer's
        ``swipe_duration_ms - duration_jitter_ms >= 1`` invariant at small values.
        """
        if config.swipe_duration_ms is None:
            return Humanizer()
        base = HumanizerConfig.from_env()
        return Humanizer(
            replace(
                base,
                swipe_duration_ms=config.swipe_duration_ms,
                duration_jitter_ms=0,
            )
        )

    # -- public API -----------------------------------------------------------

    def run(self) -> None:
        """Play until :attr:`LiveConfig.max_games` games have ended (0 = forever).

        Never-crash outer loop (overnight hardening): a transient failure
        anywhere in the session (device hiccup, adb drop, parse crash, a
        failed restart flow) finalizes the open game, is recovered in place
        (:meth:`SessionRecovery.recover`: incident + alert + reconnect + backoff)
        and the loop re-acquires the game from a fresh screen -- bounded by
        :attr:`LiveConfig.recover_max_per_hour`. Games that ended in error
        never stop the session either: the next game simply starts.

        Raises:
            FatalIncident: truly unrecoverable states only (AUTH_REQUIRED
                without auto-login, CRASH, persistent unknown screens) or
                any FATAL once the recovery budget is exhausted;
                ``run_session`` maps it to exit(2).
            LiveLoopError: internal contract violation (CRASH path).

        Any exception that ends the session mid-game (FATAL incident,
        deliberate stop, crash) first finalizes the open game via
        ``GameEnded`` (see module docs) and is then re-raised unchanged.
        """
        # P5 precedence: resume_on_start overrides reset_on_start ONLY when a
        # resumable in-progress game is detected; otherwise the reset flow
        # runs exactly as before.
        if not self._resume_on_start():
            self._reset_on_start()
        self._start_grace()
        self._start_bg_detect()
        try:
            self._run_games()
        finally:
            self._stop_bg_detect()

    def _run_games(self) -> None:
        """The game loop proper (see :meth:`run`); the async worker wraps it."""
        games_done = 0
        state: _GameState | None = None
        while True:
            try:
                if state is None:
                    state = self._acquire_initial_game()
                end_reason = self._play_game(state)
                log.info(
                    "live.game_finished",
                    game_id=state.game_id,
                    end_reason=end_reason,
                    score=state.score_total,
                    moves=state.ply,
                )
                games_done += 1
                self._note_game_ended(state, end_reason)
                if end_reason == HELD_BEFORE_WIN_REASON:
                    # --stop-before-win held the board one swipe from 2048: the
                    # human takes over, so the session ALWAYS stops here (never
                    # autorestart, regardless of max_games).
                    log.info("live.session_held", games=games_done)
                    return
                if self.config.max_games and games_done >= self.config.max_games:
                    log.info("live.session_done", games=games_done)
                    return
                # After a banked 2048 win the FLAG_SECURE win popup is still
                # on screen and screencap is blind: tell the supervisor so it
                # treats a blind probe as the EXPECTED win surface instead of
                # escalating to AUTH_REQUIRED / auto-login.
                t0, frame = self.supervisor.start_new_game(
                    after_win=(end_reason == "win_2048")
                )
                self._notify("INFO", "RESTARTED", "🔁 Рестарт — почав нову гру")
                state = self._begin_game(self._settled_board(None, frame), t0)
            except _GameReset as reset:
                # Old game already closed as 'interrupted' by the auth path.
                log.warning(
                    "live.game_reset_by_app",
                    old_game_id=state.game_id if state is not None else None,
                )
                state = self._begin_game(reset.board, reset.t0)
            except BaseException as exc:
                # FATAL / stop / crash mid-game: finalize the summary row
                # from the last verified board first, in every case.
                if state is not None and not state.ended:
                    self._abort_open_game(state, exc)
                    games_done += 1
                if not self._recovery.recoverable(exc, games_done):
                    raise
                self._recovery.recover(exc)  # re-raises `exc` when over budget
                state = None  # re-acquire from a fresh screen next iteration

    # -- game lifecycle ---------------------------------------------------------

    def _resume_on_start(self) -> bool:
        """Adopt the in-progress game already on screen instead of wiping it (P5).

        Crash-restart losslessness: with :attr:`LiveConfig.resume_on_start`
        set, session start first probes the screen (auth-tolerant, ZERO taps)
        and -- when a healthy GAME screen shows a board that is neither fresh
        nor dead -- seeds the first game from that board via
        :attr:`_initial_seed`, SKIPPING the «Нова гра» reset entirely. Returns
        True exactly then; every other outcome returns False and the session
        proceeds through the unchanged :meth:`_reset_on_start` flow.

        PRECEDENCE (P5): ``resume_on_start`` overrides ``reset_on_start`` ONLY
        when a resumable in-progress game is detected:

        * screen not GAME (popup / off / unknown / bank home) -> False,
          normal reset/acquire behavior unchanged;
        * AUTH_REQUIRED -> the EXISTING opt-in auto-login runs FIRST
          (:meth:`AuthFlow.resolve_reset_auth`), THEN resume detection on the
          cleared screen; an unresolved wall -> False (the reset flow applies
          its own unchanged fail-closed auth handling);
        * parsed board is fresh (:func:`~bot2048.runner.newgame._fresh_board_reason`
          ``None`` -- a brand-new game, nothing to lose) -> False;
        * parsed board is dead (no legal move; a stale game-over board) ->
          False (the restart flow owns that screen);
        * any device/vision trouble -> WARN + False (best-effort session
          polish; the banking-app campaign never crashes here).

        SAFE CHOICE: a non-fresh board with legal moves is NEVER wiped while
        the flag is set. The resumed game's ``t0`` is ``clock()`` at adoption
        -- an honest lower bound, the real elapsed time is unobservable
        (docs/device-ui-notes.md timer semantics).
        """
        if not self.config.resume_on_start:
            return False
        log.info("live.resume_on_start_probe")
        # Raw device/detector (not the per-move timed delegates): a one-off
        # session-setup observation, unrelated to per-move latency telemetry.
        try:
            probe = probe_screen(self.device, self.detect)
            if probe.state is ScreenState.AUTH_REQUIRED:
                # Auto-login FIRST, resume detection second (P5 spec). An
                # unresolved wall (opt-out / no PIN / wall persists) falls
                # through to the reset flow's unchanged AUTH doctrine.
                if not self._auth.resolve_reset_auth(probe):
                    log.warning("live.resume_on_start_auth_unresolved")
                    return False
                probe = probe_screen(self.device, self.detect)
            if probe.state is not ScreenState.GAME:
                log.info(
                    "live.resume_on_start_not_game", state=probe.state.value
                )
                return False
            frame = (
                probe.image if probe.image is not None else self.device.screenshot()
            )
            board = self.read_board(frame)
        except Exception as exc:  # best-effort: never crash session start
            log.warning(
                "live.resume_on_start_probe_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False
        fresh_reason = _fresh_board_reason(
            board,
            starter_tiles=FRESH_STARTER_TILES,
            max_start_exponent=FRESH_MAX_START_EXPONENT,
            max_start_sum=FRESH_MAX_START_SUM,
        )
        if fresh_reason is None:
            # A brand-new board: nothing to lose -- normal reset/new-game path.
            log.info("live.resume_on_start_board_fresh", board=board)
            return False
        if engine.is_game_over(board):
            # Dead board (a stale game-over screen): resuming would end
            # instantly -- the supervisor restart flow owns this screen.
            log.info("live.resume_on_start_board_dead", board=board)
            return False
        t0 = self._clock()  # unknown real timer offset; honest lower bound
        self._initial_seed = (board, t0)
        self._notify(
            "INFO",
            "RESUME_ON_START",
            "▶️ Відновлюю гру, що вже йде на екрані — «Нова гра» не натискаю",
        )
        log.info(
            "live.resume_on_start_ok", board=board, non_fresh_reason=fresh_reason
        )
        return True

    def _reset_on_start(self) -> None:
        """Best-effort «Нова гра» tap at session start to zero the timer (#46).

        Runs BEFORE the :meth:`_start_grace` countdown (grace still guards the
        first swipe): when :attr:`LiveConfig.reset_on_start` is set the runner
        taps the in-play «Нова гра» via
        :func:`~bot2048.runner.newgame.reset_to_new_game` so the in-game
        wall-clock timer starts from this session, not from whatever game was on
        screen. On SUCCESS the verified fresh board and its ``t0`` are stashed
        in :attr:`_initial_seed` and become the first game's start state.

        AUTH WALL (#46): the bank re-routes «Нова гра» through re-authorization
        when the session token expired, so the reset probes via
        ``probe_screen`` (a FLAG_SECURE 0-byte screencap classifies instead of
        raising) and drives an AUTH_REQUIRED screen through the EXISTING opt-in
        auto-login (:meth:`AuthFlow.resolve_reset_auth`); unresolved auth just
        skips the reset (fail-closed, zero taps), never a session FATAL.

        FAIL-SAFE (banking app + overnight campaigns must never crash on the
        reset): an unsafe screen (:class:`~bot2048.runner.newgame.NewGameError`),
        a device-level error escaping the reset
        (:class:`~bot2048.device.adb.DeviceError`, e.g. a capture dying
        mid-reset) or exhausted retries (``ok`` False) only WARN -- the seed
        stays ``None`` and :meth:`_acquire_initial_game` falls back to adopting
        the current game exactly as with ``reset_on_start`` off. The in-play
        «Нова гра» coordinate can differ from the game-over popup one and NEEDS
        FRESH LIVE CALIBRATION per device (:attr:`LiveConfig.new_game_point`).
        """
        if not self.config.reset_on_start:
            return
        log.info("live.reset_on_start")
        # Raw device/detector (not the per-move timed delegates): this is a
        # one-off session-setup action, unrelated to per-move latency telemetry.
        try:
            result = reset_to_new_game(
                self.device,
                self.detect,
                self.read_board,
                new_game_point=self.config.new_game_point,
                confirm_point=self.config.confirm_point,
                resolve_auth=self._auth.resolve_reset_auth,
                sleep=self._sleep,
                clock=self._clock,
            )
        except NewGameError as exc:
            log.warning("live.reset_on_start_unsafe", reason=str(exc))
            self._notify(
                "INFO",
                "RESET_ON_START",
                "⚠️ Не вдалося обнулити таймер (небезпечний екран) — граю поточну гру",
            )
            return
        except DeviceError as exc:
            # Device-level trouble escaping the reset itself (e.g. the capture
            # died between the settle probe and the board read): the reset is
            # best-effort session polish -- WARN and play the current game
            # instead of crashing the campaign (#46).
            log.warning("live.reset_on_start_device_error", error=str(exc))
            self._notify(
                "INFO",
                "RESET_ON_START",
                "⚠️ Не вдалося обнулити таймер (помилка пристрою) — граю поточну гру",
            )
            return
        if not result or result.board is None or result.t0 is None:
            log.warning(
                "live.reset_on_start_failed",
                reason=result.reason,
                attempts=result.attempts,
            )
            self._notify(
                "INFO",
                "RESET_ON_START",
                "⚠️ Не вдалося обнулити таймер (свіжа дошка не підтвердилась) — "
                "граю поточну гру",
            )
            return
        self._initial_seed = (result.board, result.t0)
        log.info(
            "live.reset_on_start_ok", attempts=result.attempts, board=result.board
        )

    def _start_grace(self) -> None:
        """Hands-off countdown before the FIRST capture (P14).

        Emits a clear "starting in N seconds" notification (Telegram bridge +
        log) and then waits :attr:`LiveConfig.start_grace_s` seconds before the
        loop takes its first frame -- so a human still holding or unlocking the
        phone is never mistaken for a vision anomaly: nothing is captured or
        swiped during the window, so anything that happens on screen is simply
        ignored. A no-op when the grace period is zero.
        """
        grace = self.config.start_grace_s
        if grace <= 0:
            return
        self._notify(
            "INFO",
            "START_GRACE",
            f"⏳ Старт за {grace:.0f} с — прибери руки від телефона (hands off)",
        )
        log.info("live.start_grace", seconds=grace)
        remaining = grace
        while remaining > 0:
            chunk = min(5.0, remaining)
            self._sleep(chunk)
            remaining -= chunk
            if remaining > 0:
                log.info("live.start_grace_countdown", remaining_s=round(remaining, 1))

    def _acquire_initial_game(self) -> _GameState:
        # #46: a successful start-of-session reset already verified a fresh board
        # and captured the zeroed timer t0 -- adopt it directly (consumed once).
        seed = self._initial_seed
        if seed is not None:
            self._initial_seed = None
            board, t0 = seed
            log.info("live.initial_game_from_reset")
            return self._begin_game(board, t0)
        if self.config.fresh_start:
            t0, frame = self.supervisor.start_new_game()
        else:
            try:
                frame = self._capture_game_frame(None)
                t0 = self._clock()  # unknown real timer offset; honest lower bound
            except _WinPopup2048:
                # A leftover win popup from BEFORE this session (no open game
                # of ours to finalize): tap through it and start fresh. The
                # supervisor probes the screen itself and hits its own
                # WIN_POPUP_2048 branch (tap «Нова гра», settle, fresh board).
                log.info("live.stale_win_popup_at_start")
                t0, frame = self.supervisor.start_new_game(after_win=True)
            except _GameOverPopup:
                # A leftover game-over popup from BEFORE this session (no open
                # game of ours to finalize): tap through it and start fresh,
                # exactly like the stale win popup. The supervisor's own
                # GAME_OVER_POPUP branch taps «Нова гра» and settles a board.
                log.info("live.stale_game_over_popup_at_start")
                t0, frame = self.supervisor.start_new_game()
        return self._begin_game(self._settled_board(None, frame), t0)

    def _begin_game(self, board: Board, t0: float) -> _GameState:
        # A previous game's budget-ladder step (brief §3) must not leak into
        # this one: every game is an independent record attempt, so restore
        # the T0 base budget (the fresh _GameState resets the tier to T0).
        self._ladder.restore_base()
        state = _GameState(game_id=uuid.uuid4().hex, board=board, t0=t0)
        now = time.time()
        self._record(
            GameStarted(
                game_id=state.game_id,
                experiment_id=self.config.experiment_id,
                kind="live",
                strategy=self._strategy_label(),
                seed=None,
                initial_board=board,
                params={
                    "settle_delay_s": self.config.settle_delay_s,
                    "predict_gate": self.config.predict_gate,
                    "predict_gate_poll_s": self.config.predict_gate_poll_s,
                    "predict_gate_max_wait_s": self.config.predict_gate_max_wait_s,
                    "settle_stable_frames": self.config.settle_stable_frames,
                    "settle_poll_s": self.config.settle_poll_s,
                    "settle_max_wait_s": self.config.settle_max_wait_s,
                    "swipe_retries": self.config.swipe_retries,
                    "parse_retries": self.config.parse_retries,
                    "run_id": self.run_id,
                },
                ts=now,
            )
        )
        # Initial tiles as TileSpawned with negative plies (recording
        # contract). The phone's spawn order is unobservable, so tiles are
        # emitted in deterministic cell order: first tile ply -1, next -2, ...
        acc = [0] * len(board)
        tiles = 0
        for cell, exp in enumerate(board):
            if exp == 0:
                continue
            tiles += 1
            acc[cell] = exp
            self._record(
                TileSpawned(
                    game_id=state.game_id,
                    ply=-tiles,
                    spawn=Spawn(cell=cell, exponent=exp),
                    board_after=tuple(acc),
                    ts=now,
                )
            )
        if self.heartbeat is not None:
            self.heartbeat.update(game_id=state.game_id, ply=0)
        self._ops_info("GAME_STARTED", f"initial tiles: {tiles}", state.game_id, 0)
        self._notify(
            "INFO",
            "GAME_STARTED",
            f"🎮 Нова гра {state.game_id[:8]} ({tiles} стартові плитки)",
        )
        log.info("live.game_started", game_id=state.game_id, initial_tiles=tiles)
        return state

    def _play_game(self, state: _GameState) -> str:
        """Play ``state`` to an end; returns the recorded ``end_reason``."""
        while True:
            self._pause_gate()
            # The bot decides game over itself; the popup is never trusted.
            if engine.is_game_over(state.board):
                return self._end_game(state, "game_over")
            cap = self.config.max_moves_per_game
            if cap is not None and state.ply >= cap:
                return self._end_game(state, "aborted")
            try:
                abort_reason = self._swipe.play_move(state)
            except _HeldBeforeWin as held:
                # --stop-before-win: the chosen move WOULD merge to 2048 and was
                # deliberately NOT swiped. A NORMAL, intended terminal state --
                # finalize as 'held_before_win' and STOP the session so the human
                # makes the final merge (see _finish_held / _run_games).
                return self._finish_held(state, held.direction)
            except _WinPopup2048:
                # The winning swipe merged 2048 and the app covered the board
                # with «Ви зібрали 2048!» before the after-board could be
                # verified -- a HAPPY terminal state, finalized normally.
                return self._finish_win(state)
            except _GameOverPopup:
                # The fatal swipe filled the board and the app covered it with
                # «Гру завершено» before the after-board could be verified --
                # the NATURAL end of a game, finalized like an engine-decided
                # game over (not an incident, not a code bug).
                return self._finish_game_over(state)
            if abort_reason is not None:
                # A capture/verification abort -- NOT a legitimate game over and
                # NOT a code bug. Finalize with the SPECIFIC incident reason so
                # the DB never records a blanket 'error' on a game that was
                # playing fine, and log the cause on the way out (docs §5/§6).
                # The aborted move never reaches ``moves``, so its phase timing
                # + reliability counters (still intact -- reset only on the next
                # move) are emitted here instead (docs/research/telemetry_review.md
                # M4): the "slow-but-failed" move is the most valuable to debug.
                timing = self._timer.snapshot()
                move_counts = self._counters.snapshot()
                log.warning(
                    "live.game_capture_aborted",
                    game_id=state.game_id,
                    reason=abort_reason,
                    score=state.score_total,
                    moves=state.ply,
                    capture_count=move_counts["capture_count"],
                    swipe_attempts=move_counts["swipe_attempts"],
                    gate_hit=move_counts["gate_hit"],
                    **{key: round(value, 2) for key, value in timing.items()},
                )
                # Link the abort row to the incident bundle the supervisor just
                # wrote for this move (VISION_MISMATCH / MOVE_REJECTED), so
                # game_id + move_no -> live_events -> bundle is one SQL hop.
                self._ops_info(
                    "MOVE_ABORTED",
                    f"{abort_reason} at ply {state.ply}",
                    state.game_id,
                    state.ply,
                    details={
                        "timing": timing,
                        "move": move_counts,
                        "end_reason": abort_reason,
                    },
                    incident_dir=self._incidents.last_dir,
                )
                return self._end_game(state, abort_reason)
            # Budget ladder (brief §3): the move committed, the board advanced
            # -- recompute the target tier and step the budget UP exactly here.
            self._maybe_escalate_budget(state)

    def _maybe_escalate_budget(self, state: _GameState) -> None:
        """Step the search budget UP the ladder (delegates to :class:`BudgetLadder`)."""
        self._ladder.maybe_escalate(state)

    def _end_game(self, state: _GameState, end_reason: str) -> str:
        duration = max(self._clock() - state.t0, 0.0)
        # Statistics of the FINAL board (last known parsed board) -- computed
        # once, then logged, shipped as telemetry and shown in the notify line.
        stats = _board_stats(state.board)
        self._record(
            GameEnded(
                game_id=state.game_id,
                final_score=state.score_total,
                max_exponent=engine.max_exponent(state.board),
                num_moves=state.ply,
                end_reason=end_reason,
                duration_s=duration,
                ts=time.time(),
            )
        )
        state.ended = True
        # Durability of the finished game. With the live AsyncRecorder this
        # only enqueues a flush marker (the writer thread persists; the next
        # game's start is not held up); the plain SqliteRecorder flushes inline.
        try:
            self.recorder.flush()
        except StorageError as exc:
            self._storage_warn(f"flush after GameEnded failed: {exc}")
        # Postmortem artifact (best-effort; a vanished device / write failure
        # only WARNs, never disturbs the end): a FRESH screencap of the
        # terminal screen -- taken HERE, before the restart taps «Нова гра» --
        # falling back to the last parsed frame. The final board also travels IN the
        # notification text as a monospace grid (see _format_game_end_notify),
        # so no separately rendered board PNG is produced.
        frame_path = self._save_game_screen(state.game_id, end_reason)
        # Vision/device aborts additionally get an APK-packet + parsed-board
        # sidecar BESIDE the game-screen PNG just saved above (reuses that
        # artifact -- no double screencap; win_2048/game_over stay sidecar-free,
        # see DEBUG_CAPTURE_REASONS). Best-effort: write_sidecar swallows
        # everything, so finalization is never disturbed.
        if end_reason in DEBUG_CAPTURE_REASONS:
            sidecar_base = (
                frame_path
                if frame_path is not None
                else self.screens_root / f"{state.game_id}_{end_reason}.png"
            )
            self._debug.write_sidecar(
                sidecar_base,
                reason=end_reason,
                game_id=state.game_id,
                ply=state.ply,
                screenshot=frame_path.name if frame_path is not None else None,
                context={
                    "board": list(state.board),
                    "board_hex": board_to_hex(tuple(state.board)),
                    "score": state.score_total,
                    "duration_s": duration,
                },
            )
        # Game-level telemetry rides in the ops ``live_events.details_json`` field
        # -- the game-level analog of the per-move ``eval_details["timing"]``
        # bucket -- so the frozen recording/storage contracts stay untouched.
        # Game-level reliability aggregates (docs/research/telemetry_review.md N6)
        # ride alongside the tile stats so a whole campaign is comparable with
        # one SELECT over live_events -- no reconstruction from ``moves``.
        game_details: dict[str, Any] = {
            "tile_histogram": stats.histogram,
            "highest": stats.highest,
            "empty": stats.empty,
            "tile_sum": stats.total,
            "end_reason": end_reason,
        }
        game_details.update(self._game_reliability_summary(state, duration))
        self._ops_info(
            "GAME_OVER",
            f"{end_reason}: score={state.score_total} moves={state.ply} "
            f"highest={stats.highest} empty={stats.empty}",
            state.game_id,
            state.ply,
            details=game_details,
        )
        # Best-effort percentile of this score among past completed farm games
        # (read-only; never raises, skips the line when the DB has too few games).
        percentile = self._farm_percentile(state.game_id, state.score_total)
        self._notify(
            "INFO",
            "GAME_OVER",
            _format_game_end_notify(
                game_id=state.game_id,
                end_reason=end_reason,
                score=state.score_total,
                moves=state.ply,
                duration_s=duration,
                stats=stats,
                board=state.board,
                percentile=percentile,
            ),
        )
        # Attach the last device frame (guarded: a missing artifact is simply
        # not sent, the text notification above always goes out).
        gid = state.game_id[:8]
        if frame_path is not None:
            self._notify_photo(
                "INFO", "GAME_OVER", f"📸 Останній кадр — Гра {gid}", str(frame_path)
            )
        log.info(
            "live.game_ended",
            game_id=state.game_id,
            end_reason=end_reason,
            score=state.score_total,
            moves=state.ply,
            duration_s=round(duration, 1),
            final_tiles=stats.histogram,
            highest=stats.highest,
            empty=stats.empty,
            tile_sum=stats.total,
        )
        return end_reason

    def _finish_win(self, state: _GameState) -> str:
        """Finalize a game the app declared WON (``end_reason='win_2048'``).

        The popup covers the board, so the after-board of the winning move is
        unobservable and ``state`` still holds the last VERIFIED board/score
        (from before the winning swipe). The HUD score above the popup is not
        parseable by the tile pipeline (no OCR helper for the HUD digits in
        this codebase), so the final score is the honest lower bound: last
        verified score + 2048 -- the winning merge (two 1024s) is worth
        exactly ``2**WIN_EXPONENT`` points, and any extra merges in the same
        swipe are unknowable. Restart/shutdown is the caller's normal
        end-of-game path: :meth:`run` counts the game and either taps «Нова
        гра» via ``supervisor.start_new_game()`` (more games budgeted) or
        ends the session cleanly -- never a FATAL.
        """
        state.score_total += 2**WIN_EXPONENT
        self._notify(
            "INFO",
            "WIN_2048",
            f"🏆 Гра {state.game_id[:8]}: зібрано 2048! "
            f"Рахунок ≥{state.score_total} (нижня межа: останній верифікований "
            f"+2048), {state.ply} ходів",
        )
        log.info(
            "live.win_popup_2048",
            game_id=state.game_id,
            score_lower_bound=state.score_total,
            moves=state.ply,
        )
        return self._end_game(state, "win_2048")

    def _finish_held(self, state: _GameState, direction: Direction) -> str:
        """Finalize a game held one swipe from 2048 (``--stop-before-win``).

        The strategy chose ``direction`` as the winning (2048-merging) move and
        :class:`~bot2048.runner.swipe_controller.SwipeController` deliberately did
        NOT send it, so ``state`` still holds the last VERIFIED board and score
        and the phone shows a board exactly one swipe from 2048. This is a
        NORMAL, intended terminal state -- finalized via :meth:`_end_game`
        (``end_reason=`` :data:`HELD_BEFORE_WIN_REASON`) with a clear INFO
        notification naming the winning direction. :meth:`_run_games` then STOPS
        the session unconditionally (holding means the human takes over -- no
        autorestart, regardless of ``max_games``).
        """
        self._notify(
            "INFO",
            "HELD",
            f"🖐 Гра {state.game_id[:8]}: один свайп до 2048 "
            f"(напрямок {direction.name}) — тримаю борд, зроби фінальний хід сам",
        )
        log.info(
            "live.held_before_win",
            game_id=state.game_id,
            ply=state.ply,
            direction=direction.name,
            score=state.score_total,
        )
        return self._end_game(state, HELD_BEFORE_WIN_REASON)

    def _finish_game_over(self, state: _GameState) -> str:
        """Finalize a game the app declared over via «Гру завершено» popup.

        The popup covers the board AFTER the fatal swipe, so ``state`` still
        holds the last VERIFIED (dead) board -- the engine would have called
        it game over on the next loop iteration anyway (the popup only
        confirms it). This is the NATURAL end of a game (the board filled up),
        never an error, so it finalizes exactly like an engine-decided game
        over (``end_reason='game_over'`` via :meth:`_end_game`): the tile
        histogram and the end-of-game screenshot are written the same way.
        :meth:`run` then either taps «Нова гра» via
        ``supervisor.start_new_game()`` (autorestart budgeted) or ends the
        session cleanly -- never a FATAL.
        """
        log.info(
            "live.game_over_popup",
            game_id=state.game_id,
            score=state.score_total,
            moves=state.ply,
        )
        return self._end_game(state, "game_over")

    def _abort_open_game(self, state: _GameState, cause: BaseException) -> None:
        """Finalize a game killed mid-play by ``cause``; NEVER masks it.

        Emits ``GameEnded`` through :meth:`_end_game` from the last verified
        board (real score/ply). The ``end_reason`` names the ACTUAL cause (see
        :func:`_abort_reason`) so a game that was playing fine is never
        mislabeled a generic ``'error'``: ``'interrupted'`` for a deliberate
        KeyboardInterrupt; the specific incident (``'auth_required'``,
        ``'vision_mismatch'``, ``'device_lost'``, ...) for an operational
        FatalIncident; ``'error'`` only for a genuine internal crash. The cause
        is logged at WARN on the way out. No-op when the game already ended
        normally (guards against a double ``GameEnded``). Any failure while
        finalizing is logged and swallowed so the original exception keeps
        propagating; the caller re-raises ``cause`` unconditionally.
        """
        if state.ended:
            return
        end_reason = _abort_reason(cause)
        log.warning(
            "live.game_aborted",
            game_id=state.game_id,
            end_reason=end_reason,
            cause=type(cause).__name__,
            score=state.score_total,
            moves=state.ply,
        )
        try:
            self._end_game(state, end_reason)
        except Exception:
            log.exception(
                "live.abort_finalize_failed",
                game_id=state.game_id,
                cause=type(cause).__name__,
            )

    # -- on-device APK board source (fast per-move perception) --------------------

    def _start_bg_detect(self) -> None:
        """Bring up the async control-tick worker (APK mode only). Never raises.

        Builds a :class:`~bot2048.runner.bg_detect.BackgroundStateProbe` over
        :meth:`_bg_probe` when an APK :class:`BoardSource` is wired, the cadence
        is enabled (:attr:`LiveConfig.apk_detect_every_moves` > 0) and a
        background interval is set (:attr:`LiveConfig.apk_detect_interval_s` > 0)
        -- unless a probe was already injected (tests), which is used verbatim.
        With the interval at ``0`` no worker is built and the loop keeps the
        purely synchronous move-based tick (:meth:`_apk_control_tick` fallback):
        the pre-async behavior, never removed.
        """
        if self._bg_detect is None:
            if self.board_source is None or self.config.apk_detect_every_moves <= 0:
                return
            if self.config.apk_detect_interval_s <= 0:
                return  # background disabled -> synchronous move-based tick only
            self._bg_detect = BackgroundStateProbe(
                self._bg_probe, interval_s=self.config.apk_detect_interval_s
            )
        self._bg_detect.start()

    def _stop_bg_detect(self) -> None:
        """Join the async worker on session end; bounded, best-effort."""
        if self._bg_detect is not None:
            try:
                self._bg_detect.close()
            except Exception as exc:  # cleanup must never mask the real outcome
                log.warning("live.bg_detect_close_failed", error=str(exc))

    def _bg_probe(self) -> ProbeResult:
        """One off-loop screen observation: ``screencap`` + ``detect_state``.

        Runs on the worker thread. Uses the RAW device/detector -- NOT the
        per-move timed delegates, whose accumulators are single-threaded -- and
        serializes the shared adb ``screencap`` channel against the main loop's
        own full-frame captures with :attr:`_capture_lock`. ``probe_screen``
        degrades device trouble into a result (never raises), so this is safe.

        SAFETY: the APK frame stream is handed to ``probe_screen`` as the
        liveness tie-breaker. On a FLAG_SECURE-blind screencap + blank
        ``ui_dump`` (the auth wall's documented signature), board packets
        still flowing prove the device is alive, so the probe returns a
        non-tap hold state instead of a false ``DEVICE_LOST`` -- the bank's
        auth surface is never routed into a tapping recovery path.
        """
        frames = (
            self._apk_frames_flowing
            if getattr(self.board_source, "last_packet_age_s", None) is not None
            else None
        )
        with self._capture_lock:
            return probe_screen(self.device, self.detect, frames_flowing=frames)

    def _apk_frames_flowing(self) -> bool:
        """True when the APK board stream delivered a packet recently.

        "Recently" = within :data:`APK_FRAMES_FRESH_WINDOW_S` -- comfortably
        above the sampler's unchanged-board heartbeat ceiling (2.55 s), so a
        static board between moves still counts as flowing. Duck-typed:
        sources without ``last_packet_age_s`` (bare test fakes) simply never
        claim liveness, which only makes the probe MORE conservative.
        """
        age_of = getattr(self.board_source, "last_packet_age_s", None)
        if age_of is None:
            return False
        age = age_of()
        return age is not None and age <= APK_FRAMES_FRESH_WINDOW_S

    def _apk_control_tick(self, state: _GameState) -> None:
        """Consume the async control-tick result (or run the sync fallback).

        The 22-byte board packet cannot see popups / the CONFIRM dialog / the
        ``FLAG_SECURE`` auth wall, so a periodic full ``screencap`` +
        ``detect_state`` must still run. It now runs ASYNCHRONOUSLY on the
        background worker (:meth:`_start_bg_detect`); here the play loop only
        does a CHEAP, non-blocking check of the worker's latest finding:

        * worker alive -> :meth:`~bot2048.runner.bg_detect.StateProbe.poll_alert`
          (a lock-guarded read). A pending NON-healthy screen is a TRIGGER: the
          loop re-confirms it synchronously via :meth:`_capture_game_frame` on
          THIS (main) thread, so all fail-closed handling is unchanged -- it
          raises :class:`_WinPopup2048` / :class:`_GameOverPopup` on a terminal
          popup, drives the auth path (FATAL / :class:`_GameReset`), or runs one
          supervisor recovery on a CONFIRM/off screen. A screen that already
          cleared simply re-confirms healthy and play continues.
        * worker NOT running (never built, or dead) -> the ORIGINAL synchronous
          tick: every :attr:`LiveConfig.apk_detect_every_moves` moves pull one
          full frame inline. So a missing/failed worker is never WORSE than the
          pre-async behavior (fail-closed).

        No-op without an APK source, with the cadence disabled, or on the game's
        opening ply (its board was just read from a full frame at acquisition).
        """
        every = self.config.apk_detect_every_moves
        if self.board_source is None or every <= 0 or state.ply == 0:
            return
        bg = self._bg_detect
        if bg is not None and bg.is_running:
            alert = bg.poll_alert()
            if alert is not None:
                log.info(
                    "live.apk_bg_alert",
                    state=alert.state.value,
                    reason=alert.reason,
                    game_id=state.game_id,
                    ply=state.ply,
                )
                # Re-observe + handle on the MAIN thread (authoritative,
                # fail-closed); raises on popup/auth/unexpected screen.
                self._capture_game_frame(state)
            return
        # Fallback: no live worker -> the pre-async synchronous periodic tick.
        if state.ply % every != 0:
            return
        log.debug("live.apk_control_tick_sync", game_id=state.game_id, ply=state.ply)
        self._capture_game_frame(state)  # raises on popup/auth/unexpected screen

    def _observe_via_apk(
        self, state: _GameState, predicted: Board
    ) -> tuple[str, Board] | None:
        """Fast APK packet gate (delegates to :class:`ObservationGate`)."""
        return self._gate._observe_via_apk(state, predicted)

    def _game_reliability_summary(
        self, state: _GameState, duration: float
    ) -> dict[str, Any]:
        """Per-game reliability aggregates for ``GAME_OVER.details_json`` (N6)."""
        telem = state.telem
        summary: dict[str, Any] = {
            "vision_fallback_moves": telem.vision_fallback_moves,
            "swipe_retry_total": telem.swipe_retry_total,
            "capture_screencap_moves": telem.capture_screencap_moves,
        }
        if telem.gate_moves:
            summary["gate_moves"] = telem.gate_moves
            summary["gate_hit_pct"] = round(100.0 * telem.gate_hits / telem.gate_moves, 1)
        if telem.move_ms:
            summary["mean_move_ms"] = round(sum(telem.move_ms) / len(telem.move_ms), 1)
            summary["p95_move_ms"] = round(_percentile(telem.move_ms, 95.0), 1)
        if duration > 0:
            summary["moves_per_s"] = round(state.ply / duration, 3)
            summary["score_per_s"] = round(state.score_total / duration, 3)
        if telem.first_1024_ply is not None:
            summary["first_1024_ply"] = telem.first_1024_ply
        if telem.first_2048_ply is not None:
            summary["first_2048_ply"] = telem.first_2048_ply
        return summary

    # -- screen handling ----------------------------------------------------------

    def _settled_board(
        self, state: _GameState | None, frame: Image | None = None
    ) -> Board:
        """Wait-for-stable-frame parse (delegates to :class:`ObservationGate`)."""
        return self._gate.settled_board(state, frame)

    def _capture_game_frame(self, state: _GameState | None) -> Image:
        """Healthy GAME frame or escalate (delegates to :class:`ObservationGate`).

        Raises :class:`_WinPopup2048` / :class:`_GameOverPopup` on a terminal
        popup (caught by ``_acquire_initial_game`` / ``_play_game``) and routes
        an AUTH_REQUIRED screen into :meth:`AuthFlow.handle_auth` -- all unchanged.
        """
        return self._gate.capture_game_frame(state)

    def _parse_or_escalate(self, frame: Image, state: _GameState | None = None) -> Board:
        """Parse ``frame`` or escalate (delegates to :class:`ObservationGate`)."""
        return self._gate.parse_or_escalate(frame, state=state)

    # -- pause gate ---------------------------------------------------------------

    def _pause_gate(self) -> None:
        """Between-moves park while ``control/pause.flag`` exists (§3.2)."""
        if not self.pause_flag.exists():
            return
        log.info("live.pause_entered", flag=str(self.pause_flag))
        self._ops_info("PAUSED", "pause flag present", None, None)
        self._notify("INFO", "PAUSED", "⏸ Пауза — можеш взяти телефон")
        # Stand the async detector down: a human may hold/unlock the phone now,
        # and a non-GAME screen seen during the pause must NOT trip a
        # fail-closed handle after resume. note_active(True) below drops any
        # alert accrued while stood down.
        if self._bg_detect is not None:
            self._bg_detect.note_active(False)
        while self.pause_flag.exists():
            self._sleep(self.config.pause_poll_s)  # heartbeat thread keeps beating
        if self._bg_detect is not None:
            self._bg_detect.note_active(True)
        self._notify("INFO", "RESUMED", "🚫 Продовжую — не чіпай телефон")
        log.info("live.pause_left")

    # -- small helpers --------------------------------------------------------------

    def _save_game_screen(self, game_id: str, end_reason: str) -> Path | None:
        """Persist the TERMINAL device frame as ``<screens_root>/<id>_<reason>.png``.

        A best-effort postmortem snapshot of the final board for EVERY game
        finalization (game over, win, or any abort). The frame is a FRESH
        screencap taken right here (:meth:`_capture_end_frame`): finalization
        runs BEFORE the restart taps «Нова гра», so the device still shows the
        real terminal screen (the filled board / the win popup over the 2048
        board). Falling back to ``self._gate.last_frame`` (the frame behind the
        latest parse) only when the fresh capture fails -- in APK-capture mode
        that frame can be as old as the game's startup board (per-move
        perception reads 22-byte packets, never full frames), which is exactly
        the staleness the fresh capture fixes. A missing frame (the device
        vanished before any capture) or a write failure is logged at WARN and
        never disturbs finalization. Returns the written path (for the
        notification to attach) or ``None``.
        """
        frame = self._capture_end_frame(game_id, end_reason)
        if frame is None:
            frame = self._gate.last_frame
            if frame is not None:
                log.warning(
                    "live.game_screen_stale_fallback",
                    game_id=game_id,
                    end_reason=end_reason,
                )
        if frame is None:
            log.warning("live.game_screen_missing", game_id=game_id, end_reason=end_reason)
            return None
        try:
            self.screens_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("live.game_screen_dir_failed", game_id=game_id, error=str(exc))
            return None
        path = self.screens_root / f"{game_id}_{end_reason}.png"
        # Reuse the incident PNG writer (same runner subpackage, best-effort:
        # it swallows Pillow/array errors into a WARN, never raises).
        incidents.write_png(path, frame)
        log.info(
            "live.game_screen_saved",
            game_id=game_id,
            end_reason=end_reason,
            path=str(path),
        )
        return path

    def _capture_end_frame(self, game_id: str, end_reason: str) -> Image | None:
        """One fresh full-frame screencap of the TERMINAL screen; best-effort.

        Called ONCE per game at finalization -- never on the move hot path. In
        APK-capture mode per-move perception reads 22-byte board packets, so
        ``self._gate.last_frame`` is whatever full frame was parsed LAST --
        typically the game's STARTUP board -- and saving it produced a stale
        end-of-game screenshot. The terminal screen is still on the device here
        (``_end_game`` runs before ``supervisor.start_new_game()`` taps «Нова
        гра»), so one raw screencap captures the real final position.

        Raw device (not the per-move timed delegates: this is a one-off
        finalization action, unrelated to per-move latency telemetry) and NO
        ``detect_state``: the screen legitimately shows a terminal popup and
        re-classifying it would just re-raise :class:`_WinPopup2048` /
        :class:`_GameOverPopup` mid-finalization. The capture lock serializes
        the screencap against the async bg-detect worker (shared adb channel).
        Any failure (device gone, 0-byte ``FLAG_SECURE`` screencap) logs a WARN
        and returns ``None``; the caller falls back to the last parsed frame.
        """
        frame: Image | None
        try:
            with self._capture_lock:
                frame = self.device.screenshot()
        except Exception as exc:
            log.warning(
                "live.end_frame_capture_failed",
                game_id=game_id,
                end_reason=end_reason,
                error=str(exc),
            )
            return None
        if frame is None or frame.size == 0:
            log.warning(
                "live.end_frame_empty", game_id=game_id, end_reason=end_reason
            )
            return None
        return frame

    def _farm_percentile(self, game_id: str, score: int) -> FarmPercentile | None:
        """Best-effort percentile of ``score`` among past completed farm games.

        Reads the telemetry DB strictly read-only via the SqliteRecorder's own
        path (``_db_path``); a ListRecorder / any recorder without one, a missing
        DB, or too few games all yield ``None`` and the notification simply omits
        the percentile line. Never writes, never raises.
        """
        db_path = getattr(self.recorder, "_db_path", None)
        if db_path is None:
            return None
        try:
            return farm_score_percentile(db_path, score, exclude_game_id=game_id)
        except Exception as exc:  # best-effort telemetry
            log.debug("live.farm_percentile_failed", game_id=game_id, error=type(exc).__name__)
            return None

    def _strategy_label(self) -> str:
        name = getattr(self.strategy, "name", None)
        return name if isinstance(name, str) else self.config.strategy_name

    def _record(self, event: GameEvent) -> None:
        """Persist one event; storage trouble is WARN, the game keeps going (§5)."""
        try:
            self.recorder.record(event)
        except StorageError as exc:
            self._storage_warn(f"{type(event).__name__} write failed: {exc}")

    def _storage_warn(self, message: str) -> None:
        self.alerts.write(
            Alert(
                level=AlertLevel.WARN,
                code=AlertCode.STORAGE_ERROR,
                message=message,
                run_id=self.run_id,
            )
        )

    def _ops_info(
        self,
        code: str,
        message: str,
        game_id: str | None,
        ply: int | None,
        details: dict[str, Any] | None = None,
        incident_dir: str | None = None,
    ) -> None:
        if self.ops is None or self.run_id is None:
            return
        try:
            self.ops.record_event(
                self.run_id,
                "INFO",
                code,
                message,
                game_id=game_id,
                ply=ply,
                details=details,
                incident_dir=incident_dir,
            )
        except StorageError as exc:
            log.warning("live.ops_event_failed", code=code, error=str(exc))

    # -- incident tracing hooks (docs/ops-monitoring.md §4) ------------------------

    def _incident_capture(self) -> Image | None:
        """FRESH full-frame screencap for an incident bundle (ring capture hook).

        Raw ``device.screenshot`` (a one-off diagnostic, not per-move latency
        telemetry) behind the shared capture lock -- bounded: if the bg-detect
        worker holds the lock for longer than a few seconds the device is in
        trouble anyway, and the sink records the failure instead of hanging the
        incident path. Exceptions propagate to :meth:`MoveRing.fresh_frame`,
        which turns them into the bundle's ``capture_error``.
        """
        if not self._capture_lock.acquire(timeout=5.0):
            raise TimeoutError("capture lock busy for 5 s (bg-detect worker stuck?)")
        try:
            return self.device.screenshot()
        finally:
            self._capture_lock.release()

    def _incident_context(self) -> dict[str, Any]:
        """Session context for ``context.json["session"]`` + the v2 alert fields.

        Everything is read defensively through ``getattr``: strategies are
        duck-typed (a scripted test strategy has no limits/engine), and a
        context accessor must never break the incident it describes.
        """
        strat: Any = self.strategy
        spec = getattr(strat, "spec", None)
        spec_hash = getattr(strat, "spec_hash", None) or getattr(spec, "spec_hash", None)
        engine: dict[str, Any] = {"kind": type(strat).__name__}
        info = getattr(strat, "engine_info", None)
        if isinstance(info, Mapping) and info:
            engine["info"] = dict(info)
        for key in ("depth", "budget_ms", "farm", "protocol"):
            value = getattr(strat, key, None)
            if isinstance(value, bool | int | float | str):
                engine[key] = value
        limits = getattr(strat, "limits", None)
        if limits is not None:
            engine["limits"] = {
                k: getattr(limits, k, None) for k in ("max_depth", "budget_ms", "max_nodes")
            }
        cfg = self.config
        return {
            "strategy": self._strategy_label(),
            "spec_hash": spec_hash if isinstance(spec_hash, str) else None,
            "engine": engine,
            "experiment_id": cfg.experiment_id,
            "capture_mode": "apk" if self.board_source is not None else "frame",
            "retry_table": {
                code.value: policy_for(code).describe()
                for code in (
                    AlertCode.MOVE_REJECTED,
                    AlertCode.VISION_MISMATCH,
                    AlertCode.SCREEN_UNEXPECTED,
                    AlertCode.DEVICE_LOST,
                    AlertCode.LOOP_ERROR,
                    AlertCode.AUTH_REQUIRED,
                )
            },
            "config": {
                "swipe_retries": cfg.swipe_retries,
                "swipe_retry_delay_s": cfg.swipe_retry_delay_s,
                "parse_retries": cfg.parse_retries,
                "occlusion_max_retries": cfg.occlusion_max_retries,
                "auth_retries": cfg.auth_retries,
                "auto_login": cfg.auto_login,
                "recover_max_per_hour": cfg.recover_max_per_hour,
                "settle_delay_s": cfg.settle_delay_s,
                "predict_gate": cfg.predict_gate,
                "swipe_duration_ms": cfg.swipe_duration_ms,
            },
        }

    def _note_game_ended(self, state: _GameState, end_reason: str) -> None:
        """Track consecutive very-short games and alert on a churn loop.

        Clean game-overs / aborts (``move_rejected`` / ``vision_mismatch`` / an
        early ``game_over``) return from :meth:`_play_game` normally and restart
        WITHOUT touching the :attr:`~LiveConfig.recover_max_per_hour` budget, so
        a persistent failure that ends every game after a handful of moves can
        silently burn the record window on hundreds of ~0-score restarts. Count
        consecutive sub-:attr:`~LiveConfig.low_score_churn_min_ply` games and
        raise ONE ERROR alert (which wakes the operator via the monitor)
        each time the streak reaches a multiple of
        :attr:`~LiveConfig.low_score_churn_limit`; any healthy-length game
        resets the streak. Fail-OPEN: the session keeps playing (never a FATAL)
        -- this only surfaces the loop, it does not stop it.
        """
        limit = self.config.low_score_churn_limit
        if limit <= 0:  # guard disabled
            return
        if state.ply >= self.config.low_score_churn_min_ply:
            self._low_score_churn_streak = 0
            return
        self._low_score_churn_streak += 1
        streak = self._low_score_churn_streak
        log.warning(
            "live.low_score_churn",
            streak=streak,
            limit=limit,
            min_ply=self.config.low_score_churn_min_ply,
            end_reason=end_reason,
            ply=state.ply,
            game_id=state.game_id,
        )
        if streak % limit != 0:
            return
        message = (
            f"{streak} consecutive games ended under "
            f"{self.config.low_score_churn_min_ply} moves "
            f"(last: {end_reason}, {state.ply} moves) -- probable churn loop "
            f"burning the run window; clean game-overs/aborts are not gated by "
            f"recover_max_per_hour"
        )
        # ONE incident path: bundle (fresh screenshot -- what IS the phone
        # showing between these short games?) + alert + live_events row.
        self._incidents.record(
            AlertCode.RESTART_LOOP,
            AlertLevel.ERROR,
            message,
            game_id=state.game_id,
            move_no=state.ply,
            attempts=streak,
            retry_policy=policy_for(AlertCode.RESTART_LOOP).describe(),
            stage="session",
            extra={
                "streak": streak,
                "limit": limit,
                "min_ply": self.config.low_score_churn_min_ply,
                "end_reason": end_reason,
            },
        )
        self._notify(
            "ERROR",
            "RESTART_LOOP",
            f"⚠️ {streak} коротких ігор поспіль (<"
            f"{self.config.low_score_churn_min_ply} ходів) — ймовірний цикл рестартів",
        )

    def _notify(self, level: str, code: str, text: str) -> None:
        if self.notify is None:
            return
        try:
            self.notify(level, code, text)
        except Exception as exc:
            log.warning("live.notify_failed", code=code, error=type(exc).__name__)

    def _notify_photo(self, level: str, code: str, text: str, photo_path: str) -> None:
        """Fire-and-forget photo notification (``text`` becomes the caption).

        Best-effort like :meth:`_notify`: a hook that predates the photo arg
        (or any send failure) is swallowed so the postmortem images can never
        disturb game finalization.
        """
        if self.notify is None:
            return
        try:
            self.notify(level, code, text, photo_path)
        except Exception as exc:
            log.warning("live.notify_photo_failed", code=code, error=type(exc).__name__)

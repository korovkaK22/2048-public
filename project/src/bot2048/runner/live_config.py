"""Live-session configuration + defaults (refactor plan R2).

Extracted VERBATIM from :mod:`bot2048.runner.live`: the runner-local
:class:`LiveConfig` dataclass (with its ``__post_init__`` validation) and the
module-level default constants it and the loop reference. ``device/config.py``
has no ``LiveConfig`` (its ``DeviceConfig`` only covers the adb transport), so
the loop parameters live here, runner-local.

:mod:`bot2048.runner.live` re-imports every symbol it still uses (and re-exports
:data:`SETTLE_DELAY_DEFAULT_S` / :class:`LiveConfig` so
``from bot2048.runner.live import LiveConfig`` keeps working for cli.py, tests
and downstream callers).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bot2048.device import ui_map
from bot2048.device.humanizer import FAST_SWIPE_DURATION_MS
from bot2048.device.ui_map import Point

#: Post-swipe pause before the first verification capture. The slide/merge
#: animation is ~115 ms (research/early_spawn_detection.md), so 0.13 s is a
#: small buffer over it: the predict-gate below tolerates early frames anyway,
#: and the legacy settle fallback stabilizes frames on its own.
SETTLE_DELAY_DEFAULT_S: float = 0.13

#: Post-swipe pause for the APK capture path specifically. The 0.13 s default
#: buffers the ~115 ms animation because a frame capture parsed mid-slide can
#: look like garbage; the APK packet gate has no such problem -- it accepts the
#: settled spawn PACKET whenever it lands (packets carry the final board, and
#: an early poll just waits for the next one inside the gate budget). So the
#: blind sleep only needs to cover the cheap part of the animation, not all of
#: it: 0.05 s trims ~80 ms/move off the critical path. Frame paths
#: (scrcpy/screencap) keep :data:`SETTLE_DELAY_DEFAULT_S`. ``--settle-delay``
#: still overrides both.
SETTLE_DELAY_APK_S: float = 0.05

#: Default swipe gesture duration (ms) for LIVE play. The game registers
#: gestures down to ~20 ms (research/swipe_throughput.md), so 45 ms keeps a
#: >2x safety margin over that floor while trimming ~35 ms/move vs the
#: humanizer fast profile's 80 ms. cli.py resolves this when neither
#: ``--swipe-duration`` nor ``BOT2048_SWIPE_MS`` is given; both still win.
#: MUST be A/B'd against the ``move_rejected`` retry rate -- the retry path
#: stays in place as the safety net for gestures the app drops.
LIVE_SWIPE_DURATION_MS: int = 45

#: Exponents the app can legally spawn (a 2 or a 4 -- measured in
#: research/early_spawn_detection.md). The predict-gate only accepts a new
#: tile with one of these values, so a foreign tile sliding OVER an empty
#: cell mid-animation can never be mistaken for the spawn.
_GATE_SPAWN_EXPONENTS: frozenset[int] = frozenset({1, 2})

#: Busy-spin floor (s) for the APK packet gate when a poll comes back empty and
#: ``apk_poll_s`` is 0. A live socket read already blocks ~``read_timeout_s``,
#: so this only matters when the link is momentarily down (poll returns at
#: once): it keeps the lull-retry loop from hot-spinning the CPU while it waits
#: out the gate budget. Small enough to still catch a just-late spawn packet.
#:
#: Latency-scout item #2 (docs/latency-scout-device-pipeline.md): a settled
#: spawn packet that lands just after an empty poll dwells in the 1-slot
#: newest-wins mailbox up to one quantum before the next poll consumes it -- at
#: 0.02 s that was ~20 ms worst case (~10 ms avg) of pure added perceive
#: latency. ``poll()`` is a lock+read with NO socket I/O, so a finer quantum is
#: nearly free (only slightly more host CPU wakeups) and the gate is accept-only
#: (exact predicted board + settled 2/4 spawn), so polling faster CANNOT
#: misclassify -- it only changes WHEN an already-validated mailbox is read.
#: 0.005 s cuts the avg mailbox dwell ~10 ms -> ~2.5 ms. (Measured golden-run
#: ``age_ms`` is ~0 for 99.5% of moves, so the mean win is tiny; the real gain
#: is the ~0.5% tail where a packet arrived mid-quantum.)
_APK_LULL_POLL_S: float = 0.005

#: Post-settle verification floor (ms) folded into
#: :meth:`LiveConfig.ponder_window_ms`: the cheap tail of one move BETWEEN
#: the settle sleep and the next ``choose()`` -- an APK packet-gate hit is one
#: socket poll (~10-30 ms measured, eval_details["move"]["apk_age_ms"]), plus
#: loop/recording overhead. Deliberately conservative (the frame predict-gate
#: path is far slower, ~150-700 ms, so a real window is usually LARGER and the
#: estimate errs toward warning too early, never too late).
_PONDER_VERIFY_FLOOR_MS: float = 40.0

#: Where end-of-game board screenshots land (runtime output, gitignored). One
#: PNG per finalized game: ``<game_id>_<end_reason>.png`` -- the last captured
#: frame of every game over / win / abort, for postmortem without the phone.
DEFAULT_GAME_SCREENS_ROOT: Path = Path("logs") / "game-screens"

#: Where mid-game on-error debug captures land (runtime output, gitignored).
#: One screenshot PNG + one ``.json`` sidecar (APK packet + parsed/predicted
#: boards) per capture -- see :mod:`bot2048.runner.debug_capture`. Bounded by
#: :attr:`LiveConfig.debug_capture_keep` (prune-oldest), so an overnight run
#: cannot fill the disk.
DEFAULT_DEBUG_CAPTURES_ROOT: Path = Path("logs") / "debug-captures"


@dataclass(frozen=True, slots=True)
class LiveConfig:
    """Parameters of one live session (runner-local; see module docs).

    Attributes:
        experiment_id: groups the recorded games (recording contract).
        strategy_name: label stored in ``GameStarted`` when the strategy
            object has no ``name`` (kept for CLI symmetry).
        max_games: stop after this many ENDED games (game_over/aborted/error);
            ``0`` = run indefinitely (until killed) -- overnight convention.
        max_moves_per_game: safety cap; ``None`` = unlimited. A capped game
            ends with ``end_reason='aborted'``.
        settle_delay_s: extra wait after each swipe before the first
            verification capture (default :data:`SETTLE_DELAY_DEFAULT_S`, a
            buffer over the ~115 ms merge/spawn animation).
        predict_gate: fast verification tier -- accept the FIRST post-swipe
            frame that parses to the engine prediction plus exactly one
            settled 2/4 spawn (:meth:`LiveRunner._predict_gate`) instead of
            waiting for consecutive identical full captures. ``False``
            restores the pre-gate behavior exactly.
        predict_gate_poll_s: pause between predict-gate captures.
        predict_gate_max_wait_s: total predict-gate budget per swipe attempt;
            on timeout verification falls back to the legacy stable-frame
            settle below (zero reliability regression: the gate can only
            accept a board the legacy verification would also accept).
        settle_stable_frames: consecutive captures that must parse to the
            SAME board before a frame counts as settled (1 = no stabilizing,
            pre-stable-frame behavior).
        settle_poll_s: pause between captures while waiting for stability.
        settle_max_wait_s: total budget of one stabilization; on timeout the
            last parse is used as-is (a genuinely inconsistent screen still
            surfaces as VISION_MISMATCH downstream).
        parse_retries: stabilized captures per verification before the board
            diff is considered final.
        swipe_retries: extra swipe attempts when the board did not change
            (then MOVE_REJECTED). Mirrors ``escalation.POLICIES`` (§5).
        swipe_retry_delay_s: pause between swipe retries.
        auto_login: opt-in PIN entry on the auth wall. Default False =
            doctrine: AUTH_REQUIRED -> FATAL, a human unlocks the phone.
        auth_settle_s: wait after the last PIN digit before re-probing.
        auth_retries: total PIN-entry attempts while the wall persists
            (unattended runs: enter, wait, re-check, re-enter; only after
            all attempts fail does AUTH_REQUIRED escalate FATAL).
        auth_popup_restart_limit: cap on CONSECUTIVE auth->terminal-popup
            restarts. A win/game-over popup surfacing right under the auth wall
            (e.g. auto-login typed the PIN over a «Ви зібрали 2048!» card) is a
            KNOWN, button-bearing terminal state -- NOT a dangerous unknown --
            so it is funneled into the normal auth-tolerant restart path
            (``supervisor.start_new_game`` / the game loop's popup exception)
            instead of a FATAL. This bounds a pathological
            popup->restart->popup cycle where nothing ever progresses: after
            this many terminal popups reached through the auth wall with NO
            committed move in between, the run FATALs (fail-closed). Any
            committed move resets the count. Must be >= 1.
        pause_poll_s: poll period of the ``control/pause.flag`` gate.
        recover_max_per_hour: never-crash outer loop -- transient session
            failures (adb drop, parse crash, a failed restart flow) are
            recovered in place up to this many times per sliding hour
            (mirrors the lifecycle restart budget); ``0`` disables recovery
            (pre-hardening behavior). Truly unrecoverable FATALs
            (AUTH_REQUIRED, CRASH, persistent unknown screens) always
            propagate.
        recover_backoff_s: pause after each session recovery before
            re-acquiring the game from a fresh screen.
        fresh_start: start the session with the restart flow instead of
            adopting whatever game is on screen (research mode needs clean
            initial-tile statistics).
        start_grace_s: hands-off countdown before the FIRST capture/swipe
            (P14). The runner notifies "starting in N seconds" and waits this
            long so a human still holding/unlocking the phone is not mistaken
            for a vision anomaly (nothing is captured during the window).
            ``0`` disables it.
        apk_gate_max_wait_s: with an APK :class:`BoardSource` injected, the
            budget spent polling packets for one whose board is the engine
            prediction plus a settled 2/4 spawn before giving up and falling
            back to the full-frame verification (zero reliability regression:
            the packet gate can only accept a board the frame path would too).
        apk_poll_s: pause between packet polls (the socket read already blocks
            on its own timeout, so ``0`` -- poll as fast as packets arrive --
            is the default).
        apk_detect_every_moves: with an APK :class:`BoardSource`, enables the
            periodic ``detect_state`` control tick that catches popups / auth /
            foreign screens the 22-byte packet cannot see (on top of the
            on-suspicion full frame the packet's not-in-game / secure flags
            trigger). By default that tick runs ASYNCHRONOUSLY on a background
            thread (see :attr:`apk_detect_interval_s`), so the per-move loop
            only cheaply CONSUMES its result; the move count is used solely by
            the synchronous FALLBACK path (when the background worker is not
            running) -- every this many moves that fallback pulls one full
            frame inline. ``0`` disables the periodic tick entirely
            (suspicion-only).
        apk_detect_interval_s: period of the ASYNCHRONOUS background
            ``detect_state`` worker (:class:`~bot2048.runner.bg_detect.BackgroundStateProbe`)
            when an APK :class:`BoardSource` is injected and
            :attr:`apk_detect_every_moves` is enabled. The worker pulls a full
            ``screencap`` off the play loop every this many seconds so the
            visible board never freezes on the ~0.65 s WiFi capture; the loop
            consumes its findings fail-closed. ``0`` disables the background
            worker and reverts to the purely synchronous move-based tick
            (:attr:`apk_detect_every_moves`) -- the pre-async behavior.
        occlusion_retry_delay_s: pause between the slow occlusion retries (see
            the field comment). Default 3.0 s.
        occlusion_max_retries: how many fresh-recapture retries to spend waiting
            out a transient physical occlusion before aborting VISION_MISMATCH.
            Default 5 (3.0 s x 5 ~= 15 s worst case); ``0`` disables the slow
            stage (immediate abort).
        reset_on_start: tap «Нова гра» once at session start (#46) to zero the
            in-game timer before the first game, via
            :func:`~bot2048.runner.newgame.reset_to_new_game`. An auth wall on
            the route (token expiry re-routes «Нова гра» through re-auth) is
            driven through the EXISTING opt-in auto-login
            (:meth:`~bot2048.runner.auth_flow.AuthFlow.resolve_reset_auth`).
            BEST-EFFORT: on an unsafe / unresolved-auth screen
            (:class:`~bot2048.runner.newgame.NewGameError`), a device-level
            error (:class:`~bot2048.device.adb.DeviceError`, e.g. the
            FLAG_SECURE 0-byte screencap) or exhausted retries it only WARNs
            and the loop acquires the current game exactly as with
            ``reset_on_start=False`` -- the campaign never crashes on the
            reset. On success the verified fresh board and its ``t0`` seed the
            first game (the zeroed timer).
        new_game_point: calibrated in-play «Нова гра» tap point used by the
            start-of-session reset. Defaults to :data:`bot2048.device.ui_map.NEW_GAME`
            (the game-over popup button, measured on the reference device); the
            IN-PLAY button may sit elsewhere on a given device, so it stays
            configurable and NEEDS FRESH LIVE CALIBRATION per device.
        confirm_point: calibrated «Так, нова гра» confirm tap point for the
            reset (default :data:`bot2048.device.ui_map.CONFIRM_YES`).
        stop_before_win: safe "brink" mode. When set, the moment the strategy's
            chosen move WOULD create a 2048 tile (the already-computed predicted
            post-move board has max exponent >= ``WIN_EXPONENT``) the runner does
            NOT send that swipe: it finalizes the game as ``'held_before_win'``
            and STOPS the session (no autorestart, regardless of ``max_games``),
            leaving the phone board exactly one swipe from 2048 for a human to
            make the final merge and observe the score. Default False -- campaigns
            must never accidentally hold. Adds no per-move latency: the check
            reuses the board the loop already computes before every swipe.
    """

    experiment_id: str = "live"
    strategy_name: str = "expectimax"
    max_games: int = 1
    max_moves_per_game: int | None = None
    start_grace_s: float = 10.0
    settle_delay_s: float = SETTLE_DELAY_DEFAULT_S
    predict_gate: bool = True
    predict_gate_poll_s: float = 0.05
    predict_gate_max_wait_s: float = 1.0
    settle_stable_frames: int = 2
    settle_poll_s: float = 0.13
    settle_max_wait_s: float = 1.2
    parse_retries: int = 3
    swipe_retries: int = 4
    swipe_retry_delay_s: float = 0.25
    auto_login: bool = False
    auth_settle_s: float = 3.0
    auth_retries: int = 2
    auth_popup_restart_limit: int = 3
    pause_poll_s: float = 2.0
    fresh_start: bool = False
    recover_max_per_hour: int = 6
    recover_backoff_s: float = 5.0
    apk_gate_max_wait_s: float = 0.8
    apk_poll_s: float = 0.0
    #: Override the swipe gesture duration (ms), jitter-free, for live A/B of
    #: swipe speed vs stability. ``None`` -> keep the humanizer profile default
    #: (fast = 80 ms); an explicit value overlays a FIXED duration on whatever
    #: pacing profile the env selects (same effect as ``BOT2048_SWIPE_MS``).
    swipe_duration_ms: int | None = None
    apk_detect_every_moves: int = 12
    apk_detect_interval_s: float = 10.0
    #: Transient-occlusion slow retry before a VISION_MISMATCH abort. A physical
    #: object (e.g. a cat's paw) can occlude 1-2 board cells for a few seconds;
    #: the fast animation loop then parses a garbage board and the game would be
    #: LOST as ``vision_mismatch`` (a real game died this way at 1136 moves /
    #: 19720). Since the occlusion is transient and the leaderboard ranks SCORE
    #: not time, the runner instead WAITS it out: on a mismatch whose cells are
    #: unreadable (color-fallback / low-confidence), it re-captures a FRESH frame
    #: up to :attr:`occlusion_max_retries` times, sleeping this long between
    #: tries, and commits the move the instant a fully-readable frame reconciles.
    #: The default 3.0 s x 5 retries = "wait 3 s, fresh recapture, x5, then
    #: error" (~15 s worst case). ``0`` retries disables the slow stage (immediate
    #: abort, the pre-feature behavior). Only the OCCLUSION case slow-retries: a
    #: fully-readable but genuinely different board still aborts at once.
    occlusion_retry_delay_s: float = 3.0
    occlusion_max_retries: int = 5
    #: On-error debug captures (feat/debug-screenshot-on-error). When a move
    #: dies on vision (a ``vision_mismatch`` abort, the occlusion retry/give-up)
    #: or the game aborts on a device/screen incident, save a postmortem bundle
    #: -- a fresh screenshot PNG + a ``.json`` sidecar with the parsed/predicted
    #: boards and the latest APK board packet -- so "what did the board actually
    #: look like?" is answerable after the fact (the decorative cat-mascot vs a
    #: mid-animation frame vs a real vision bug). Captures are fire-and-forget
    #: background threads and NEVER disturb the loop. Default ON.
    debug_capture_on_error: bool = True
    #: Bound the debug-captures directory: keep only the NEWEST this many files
    #: (a capture is 2 files -- PNG + sidecar -- so 200 files ~= 100 captures),
    #: pruning the oldest, so an overnight campaign can't fill the disk.
    debug_capture_keep: int = 200
    reset_on_start: bool = False
    #: RESUME an in-progress game found on screen at session start (P5 --
    #: lossless crash-restarts): instead of tapping «Нова гра» and WIPING a
    #: live board, the runner adopts it and plays on. Precedence: overrides
    #: ``reset_on_start`` ONLY when the start-of-session probe shows a healthy
    #: GAME screen whose parsed board is neither fresh (not just 2/4 starter
    #: tiles) nor dead; in EVERY other case (fresh board, popup/off/unknown
    #: screen, unresolved auth, any probe/parse trouble) the session falls
    #: through to the unchanged ``reset_on_start`` behavior. An AUTH_REQUIRED
    #: screen is first driven through the EXISTING opt-in auto-login
    #: (:meth:`~bot2048.runner.auth_flow.AuthFlow.resolve_reset_auth`), THEN
    #: resume detection runs. The resumed game's timer ``t0`` is "now" -- an
    #: honest lower bound (the real elapsed time is unobservable). Default
    #: False: zero behavior change unless requested.
    resume_on_start: bool = False
    # In-play «Нова гра» / confirm tap points for the start-of-session reset
    # (#46). Defaults are the reference-device ui_map points; the in-play New
    # game button can differ from the popup one -- CALIBRATE LIVE per device.
    new_game_point: Point = ui_map.NEW_GAME
    confirm_point: Point = ui_map.CONFIRM_YES
    stop_before_win: bool = False
    #: Low-score churn guard (reliability). Clean game-overs / aborts
    #: (``move_rejected`` / ``vision_mismatch`` / an early ``game_over``) return
    #: normally and restart WITHOUT consuming the :attr:`recover_max_per_hour`
    #: budget, so a persistent failure can silently restart hundreds of
    #: ~0-score games and burn the record window unnoticed. A game that ends
    #: with FEWER committed moves than :attr:`low_score_churn_min_ply` counts as
    #: "churn"; once :attr:`low_score_churn_limit` such games end BACK-TO-BACK
    #: the runner raises an ERROR alert (waking the operator via the
    #: monitor) and re-raises it every further ``low_score_churn_limit`` games
    #: while the streak holds. Any healthy-length game resets the streak.
    #: Fail-OPEN: this only SURFACES the loop, it never stops the session (no
    #: FATAL, no throttling). ``low_score_churn_limit == 0`` disables the guard.
    #: Defaults are deliberately conservative -- a real game plays hundreds to
    #: thousands of moves, so 8 consecutive sub-20-move games is essentially
    #: impossible unless something is systematically broken.
    low_score_churn_min_ply: int = 20
    low_score_churn_limit: int = 8
    # Mid-game budget escalation (docs/live-record-attempt-brief.md §5.2): once
    # a game survives past the champion's ~2067-move median, OR the board is
    # late-game (few empty cells -> low branching), raise the go strategy's
    # ``budget_ms`` to 500-1000 ms for a deep, decisive endgame search exactly
    # where the record is won. ALL knobs default to None = feature OFF, zero
    # behavior change. Only acts on a budget-mode GoExpectimaxStrategy.
    #: Escalate once ``state.ply`` reaches this many moves (the ~2067 median
    #: trigger). ``None`` disables the ply trigger.
    escalate_after_ply: int | None = None
    #: Escalate once the board has at most this many empty cells (the
    #: late-game / sparse-branching trigger). ``None`` disables it.
    escalate_when_empty_le: int | None = None
    #: The raised per-move budget (ms) applied when either trigger fires
    #: (brief: 500-1000). ``None`` disables escalation entirely.
    escalate_budget_ms: float | None = None
    # 3-tier exponential budget LADDER (briefs/live-pipeline-timing.md §3):
    # T0 bulk = the base ``--budget-ms`` (hidden under the ~250 ms swipe
    # cadence), T1 late-game past the champion median (~2067 moves, ~2000 ms),
    # T2 endgame in the death-zone (few empty cells, up to ~9000 ms). Each
    # move the loop computes the target tier from the CURRENT phase and steps
    # the go strategy's ``budget_ms`` UP when the tier rises -- monotonic, no
    # step-down mid-game (see :meth:`LiveRunner._maybe_escalate_budget`).
    # ALL knobs default to None = ladder OFF, base budget throughout, zero
    # behavior change. The legacy single-step ``escalate_*`` knobs above map
    # onto the ladder (see the ``ladder_*`` properties): the ply trigger
    # becomes T1, the empty-cells trigger becomes T2, both at
    # ``escalate_budget_ms`` -- identical observable behavior to the old
    # one-shot hook. Thresholds are tunable (they converge after the
    # doom A/B); recommended starting point: T1 at ply 2067 /
    # 2000 ms, T2 at <= 3 empty / 9000 ms.
    #: T1 trigger: the game reached this many moves (the ~2067 champion
    #: median -- "going for the record"). ``None`` disables T1.
    late_after_ply: int | None = None
    #: T1 per-move budget (ms; brief: ~2000). ``None`` disables T1.
    late_budget_ms: float | None = None
    #: T2 trigger: the board has at most this many empty cells (death-zone /
    #: near cash-out, sparse branching). ``None`` disables T2.
    endgame_when_empty_le: int | None = None
    #: T2 per-move budget (ms; brief: up to ~9000 -- deepest search exactly
    #: where the record is won or lost). ``None`` disables T2.
    endgame_budget_ms: float | None = None
    #: T2 MOVE-COUNT gate: the endgame tier fires only once ``state.ply`` also
    #: reaches this many moves, so a low-empties board only counts as the
    #: death-zone when the game is genuinely LATE. Proven live bug: a fresh
    #: board transiently drops to <= 4 empty cells around ply 20-40 before the
    #: net opens up, so an empties-ONLY T2 fired at ply 22 and ran the whole
    #: game at the slow endgame budget (halved throughput). ``None`` defaults
    #: (via :attr:`ladder_endgame_after_ply`) to the T1 median so early dips
    #: can NEVER trigger T2; set explicitly to gate T2 even later.
    endgame_after_ply: int | None = None

    # -- ladder resolution (new knobs first, legacy escalate_* as fallback) ----
    #
    # The old one-shot escalation is a degenerate ladder: ply trigger -> T1,
    # empty-cells trigger -> T2, both stepping to ``escalate_budget_ms``. The
    # properties below resolve the EFFECTIVE tier knobs so live.py has a
    # single code path and ``--escalate-*`` keeps working unchanged.

    @property
    def ladder_late_after_ply(self) -> int | None:
        """Effective T1 ply threshold (new knob, else legacy ``escalate_after_ply``)."""
        if self.late_after_ply is not None:
            return self.late_after_ply
        return self.escalate_after_ply

    @property
    def ladder_late_budget_ms(self) -> float | None:
        """Effective T1 budget (new knob, else legacy ``escalate_budget_ms``)."""
        if self.late_budget_ms is not None:
            return self.late_budget_ms
        return self.escalate_budget_ms

    @property
    def ladder_endgame_when_empty_le(self) -> int | None:
        """Effective T2 empty-cells threshold (new knob, else legacy)."""
        if self.endgame_when_empty_le is not None:
            return self.endgame_when_empty_le
        return self.escalate_when_empty_le

    @property
    def ladder_endgame_budget_ms(self) -> float | None:
        """Effective T2 budget (new knob, else legacy ``escalate_budget_ms``)."""
        if self.endgame_budget_ms is not None:
            return self.endgame_budget_ms
        return self.escalate_budget_ms

    @property
    def ladder_endgame_after_ply(self) -> int | None:
        """Effective T2 move-count gate; ``None`` = ungated by ply.

        Explicit ``endgame_after_ply`` wins; otherwise the endgame tier
        inherits the T1 median (:attr:`ladder_late_after_ply`) so a transient
        early low-empties dip can never fire T2 (the live ply-22 bug). Only
        when NO ply threshold exists at all (the legacy empties-only
        ``escalate_when_empty_le`` config) does this stay ``None`` and T2 keep
        its original ungated behavior -- back-compat for the old one-shot hook.
        """
        if self.endgame_after_ply is not None:
            return self.endgame_after_ply
        return self.ladder_late_after_ply

    def ponder_window_ms(self) -> float:
        """Estimated per-move window (ms) where engine compute is FREE.

        With ``--prefetch`` the go engine speculates the next move in the
        background while the loop is busy executing the current one; every
        millisecond of that busy time hides engine search off the critical
        path. This estimates the busy window from THIS config's own knobs:

        * the swipe gesture itself (:attr:`swipe_duration_ms`, else the fast
          profile's :data:`~bot2048.device.humanizer.FAST_SWIPE_DURATION_MS`);
        * the blind post-swipe settle sleep (:attr:`settle_delay_s`);
        * :data:`_PONDER_VERIFY_FLOOR_MS` -- the cheap verification tail
          (APK packet-gate hit + loop overhead), deliberately conservative.

        Used as the recommended CEILING for the base (T0) ``--budget-ms``: a
        base budget above this window cannot finish its speculation between
        moves, so the search leaks back onto the per-move path (see the
        cli.py startup warning). The T1/T2 ladder tiers are MEANT to exceed
        it -- late-game depth is bought with latency on purpose. Estimate
        only, for warnings/docs -- never a clamp.
        """
        swipe_ms = (
            float(self.swipe_duration_ms)
            if self.swipe_duration_ms is not None
            else float(FAST_SWIPE_DURATION_MS)
        )
        return swipe_ms + self.settle_delay_s * 1000.0 + _PONDER_VERIFY_FLOOR_MS

    def __post_init__(self) -> None:
        if self.max_games < 0:
            raise ValueError(f"max_games must be >= 0 (0 = unlimited), got {self.max_games}")
        if self.auth_retries < 1:
            raise ValueError(f"auth_retries must be >= 1, got {self.auth_retries}")
        if self.auth_popup_restart_limit < 1:
            raise ValueError(
                "auth_popup_restart_limit must be >= 1, got "
                f"{self.auth_popup_restart_limit}"
            )
        if self.recover_max_per_hour < 0:
            raise ValueError(
                f"recover_max_per_hour must be >= 0, got {self.recover_max_per_hour}"
            )
        if self.recover_backoff_s < 0:
            raise ValueError(f"recover_backoff_s must be >= 0, got {self.recover_backoff_s}")
        if self.parse_retries < 1:
            raise ValueError(f"parse_retries must be >= 1, got {self.parse_retries}")
        if self.settle_stable_frames < 1:
            raise ValueError(
                f"settle_stable_frames must be >= 1, got {self.settle_stable_frames}"
            )
        if self.settle_poll_s < 0:
            raise ValueError(f"settle_poll_s must be >= 0, got {self.settle_poll_s}")
        if self.settle_max_wait_s < 0:
            raise ValueError(f"settle_max_wait_s must be >= 0, got {self.settle_max_wait_s}")
        if self.predict_gate_poll_s < 0:
            raise ValueError(
                f"predict_gate_poll_s must be >= 0, got {self.predict_gate_poll_s}"
            )
        if self.predict_gate_max_wait_s < 0:
            raise ValueError(
                f"predict_gate_max_wait_s must be >= 0, got {self.predict_gate_max_wait_s}"
            )
        if self.swipe_retries < 0:
            raise ValueError(f"swipe_retries must be >= 0, got {self.swipe_retries}")
        if self.start_grace_s < 0:
            raise ValueError(f"start_grace_s must be >= 0, got {self.start_grace_s}")
        if self.apk_gate_max_wait_s < 0:
            raise ValueError(
                f"apk_gate_max_wait_s must be >= 0, got {self.apk_gate_max_wait_s}"
            )
        if self.apk_poll_s < 0:
            raise ValueError(f"apk_poll_s must be >= 0, got {self.apk_poll_s}")
        if self.swipe_duration_ms is not None and self.swipe_duration_ms < 1:
            raise ValueError(
                f"swipe_duration_ms must be >= 1 when set, got {self.swipe_duration_ms}"
            )
        if self.apk_detect_every_moves < 0:
            raise ValueError(
                f"apk_detect_every_moves must be >= 0, got {self.apk_detect_every_moves}"
            )
        if self.apk_detect_interval_s < 0:
            raise ValueError(
                f"apk_detect_interval_s must be >= 0, got {self.apk_detect_interval_s}"
            )
        if self.occlusion_retry_delay_s < 0:
            raise ValueError(
                "occlusion_retry_delay_s must be >= 0, got "
                f"{self.occlusion_retry_delay_s}"
            )
        if self.occlusion_max_retries < 0:
            raise ValueError(
                f"occlusion_max_retries must be >= 0, got {self.occlusion_max_retries}"
            )
        if self.debug_capture_keep < 1:
            raise ValueError(
                f"debug_capture_keep must be >= 1, got {self.debug_capture_keep}"
            )
        if self.low_score_churn_min_ply < 0:
            raise ValueError(
                "low_score_churn_min_ply must be >= 0, got "
                f"{self.low_score_churn_min_ply}"
            )
        if self.low_score_churn_limit < 0:
            raise ValueError(
                "low_score_churn_limit must be >= 0, got "
                f"{self.low_score_churn_limit}"
            )
        if self.escalate_after_ply is not None and self.escalate_after_ply < 1:
            raise ValueError(
                f"escalate_after_ply must be >= 1 when set, got {self.escalate_after_ply}"
            )
        if self.escalate_when_empty_le is not None and self.escalate_when_empty_le < 0:
            raise ValueError(
                "escalate_when_empty_le must be >= 0 when set, got "
                f"{self.escalate_when_empty_le}"
            )
        if self.escalate_budget_ms is not None and not self.escalate_budget_ms > 0:
            raise ValueError(
                f"escalate_budget_ms must be > 0 when set, got {self.escalate_budget_ms}"
            )
        if self.late_after_ply is not None and self.late_after_ply < 1:
            raise ValueError(
                f"late_after_ply must be >= 1 when set, got {self.late_after_ply}"
            )
        if self.late_budget_ms is not None and not self.late_budget_ms > 0:
            raise ValueError(
                f"late_budget_ms must be > 0 when set, got {self.late_budget_ms}"
            )
        if self.endgame_when_empty_le is not None and self.endgame_when_empty_le < 0:
            raise ValueError(
                "endgame_when_empty_le must be >= 0 when set, got "
                f"{self.endgame_when_empty_le}"
            )
        if self.endgame_budget_ms is not None and not self.endgame_budget_ms > 0:
            raise ValueError(
                f"endgame_budget_ms must be > 0 when set, got {self.endgame_budget_ms}"
            )
        if self.endgame_after_ply is not None and self.endgame_after_ply < 1:
            raise ValueError(
                f"endgame_after_ply must be >= 1 when set, got {self.endgame_after_ply}"
            )
        # The ladder must climb: a T2 budget below T1 would make the deepest
        # phase SHALLOWER than late-game -- reject the inverted configuration
        # (equal is fine: the legacy escalate_budget_ms maps onto both tiers).
        late = self.ladder_late_budget_ms
        endgame = self.ladder_endgame_budget_ms
        if late is not None and endgame is not None and endgame < late:
            raise ValueError(
                "endgame budget must be >= late budget (monotonic ladder), got "
                f"T2={endgame} < T1={late}"
            )

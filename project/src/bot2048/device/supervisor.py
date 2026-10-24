"""Live-session supervisor: the ERROR-recovery FSM + FATAL stop.

Escalation ladder (docs/ops-monitoring.md §5):

* **WARN** -- the runner retries in place (budgets in
  :data:`bot2048.runner.escalation.POLICIES`); never reaches this class.
* **ERROR** -- :meth:`Supervisor.handle_error`: incident bundle + ERROR
  alert, then exactly ONE safe recovery cycle (re-classify -> allowed action
  -> re-verify). Success -> ``recovered`` event, game continues.
* **FATAL** -- :meth:`Supervisor.fatal`: no taps ever again, bundle + FATAL
  alert, raise :class:`FatalIncident`; the top-level handler exits(2) and the
  operator is notified via ``python -m bot2048.tgnotify watch``.

Allowed recovery actions -- taps ONLY at calibrated anchors in states where
they are visible (banking app, everything else is forbidden):

========================  ====================================================
state                     action
========================  ====================================================
GAME (board valid)        done -- recovered
SCREEN_OFF                ``input keyevent KEYCODE_WAKEUP`` (not an in-app tap)
CONFIRM_NEW_GAME          tap CONTINUE_GAME (never silently confirms a reset)
AUTH_REQUIRED             immediate FATAL, zero taps (auto-login stays opt-in
                          via bot2048.device.auth and is never called here)
NOT_IN_GAME / UNKNOWN     re-poll a few frames (SupervisorConfig.transient_
                          repolls); recover if one reads GAME, else FATAL --
                          a lone occluded/animating frame never ends the run,
                          a persistent non-board still does (blind taps forbidden)
DEVICE_LOST               immediate FATAL -- blind taps forbidden
========================  ====================================================

AUTH_REQUIRED facts (measured live 2026-07-10): the bank re-locks after
~9 min WITHOUT touches; continuous play never triggers it, but any pause
longer than ~5 min must expect the auth wall. The auth screen is
FLAG_SECURE: screencap returns 0 bytes and ``ui_dump()`` is the only
observation channel.

Game lifecycle across auth (verified by the user, 2026-07-10):

* screen lock (display off/on)  -> the game SURVIVES; resume from the
  screen is valid.
* swipe-kill from recents       -> the game is RESET (cold start -> auth
  -> brand-new game).

Post-auth recovery rule: when AUTH_REQUIRED was caused by a process kill,
the previous game in storage is over -- close it as
``end_reason='interrupted'`` (``OpsRepository.mark_interrupted_games``,
already done by lifecycle startup) and start a fresh game with full
re-recognition; NEVER try to "finish" the old board. Classification after
auth: score == 0 AND timer ~= 0 AND exactly 2 starter tiles -> new game
(there was a kill); score/board preserved -> it was a mere idle/lock,
resume is valid. See docs/edge-cases.md and docs/device-ui-notes.md.

Game-timer semantics (3 independent measurements agree to the second): the
in-game timer is PURE WALL-CLOCK from the moment the confirm tap («Так,
нова гра») lands. :meth:`start_new_game` therefore returns
``time.monotonic()`` taken right after that tap -- the runner's own clock
mirrors the game timer exactly; timer OCR is only for cross-checks and
crash recovery (and only from a post-move frame, since the display repaints
on swipes). Timer-mode metric of record: score per wall-clock second.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol

import structlog

from bot2048.device import ui_map
from bot2048.runner import incidents
from bot2048.runner.alerts import Alert, AlertCode, AlertLevel, AlertWriter
from bot2048.runner.escalation import (
    SCREEN_UNEXPECTED_REPOLL_S,
    SCREEN_UNEXPECTED_REPOLLS,
    Classifier,
    ProbeResult,
    policy_for,
    probe_screen,
)
from bot2048.storage.errors import StorageError
from bot2048.storage.ops_schema import OpsRepository
from bot2048.vision.states import ScreenState

log = structlog.get_logger(__name__)

__all__ = [
    "AuthResolver",
    "FatalIncident",
    "Supervisor",
    "SupervisorConfig",
    "SupervisorDevice",
]

#: Delegate the restart flow calls when it lands on the AUTH_REQUIRED wall.
#: The bank re-routes «Нова гра» through re-authorization once the session
#: token expires (measured live), so a natural game-over restart can hit the
#: PIN pad instead of a fresh board. Given the auth probe the resolver must
#: clear the wall and return ``True`` (the supervisor then re-verifies the
#: fresh board) or raise :class:`FatalIncident` (fail-closed: auto-login off /
#: no PIN / attempts exhausted). PIN handling stays in the live runner
#: (:mod:`bot2048.device.auth` security contract); the supervisor never reads
#: the PIN itself and never taps blindly.
AuthResolver = Callable[[ProbeResult], bool]

#: Tap-less re-polls of the post-«Нова гра» verify probe while the screen is
#: still screencap-blind DURING A WIN RESTART (``after_win=True``). The
#: «Ви зібрали 2048!» screen is FLAG_SECURE (0-byte screencap, no ui hierarchy
#: -- docs/device-ui-notes.md) and the win->fresh-board transition can stay
#: blind for a beat after the «Нова гра» tap; re-polling is pure observation
#: (zero taps), and a screen that never resolves to GAME still FATALs after
#: the window (fail-closed). LIVE incident 20260711T210529Z: the verify probe
#: hit that blind transition and FATAL'd GAME_OVER_UNEXPECTED.
WIN_BLIND_VERIFY_REPOLLS: int = 6


class SupervisorDevice(Protocol):
    """Device surface the supervisor needs (AdbDevice satisfies it)."""

    def screenshot(self) -> incidents.Image: ...
    def ui_dump(self) -> str: ...
    def tap(self, x: int, y: int, *, sensitive: bool = False) -> None: ...
    def wake(self) -> None: ...


class FatalIncident(RuntimeError):
    """FATAL condition. Carries the alert already written to alerts.jsonl."""

    def __init__(self, alert: Alert) -> None:
        super().__init__(f"{alert.code.value}: {alert.message}")
        self.alert = alert


#: Default ``stage`` (where in the pipeline it failed) per code, for callers
#: that cannot pass one (the observation gate is shared with the vision rewrite
#: and is not touched here). ``VISION_MISMATCH`` splits on the message: the
#: gate's "board parse failed: ..." is the PARSE stage, everything else the
#: post-swipe VERIFY stage.
_DEFAULT_STAGES: dict[AlertCode, str] = {
    AlertCode.SCREEN_UNEXPECTED: "capture",
    AlertCode.DEVICE_LOST: "capture",
    AlertCode.AUTH_REQUIRED: "auth",
    AlertCode.VISION_MISMATCH: "verify",
    AlertCode.MOVE_REJECTED: "swipe",
    AlertCode.GAME_OVER_UNEXPECTED: "restart",
    AlertCode.CRASH: "session",
    AlertCode.LOOP_ERROR: "session",
    AlertCode.RESTART_LOOP: "startup",
    AlertCode.STORAGE_ERROR: "record",
}


def _default_stage(code: AlertCode, message: str) -> str:
    if code is AlertCode.VISION_MISMATCH and message.startswith("board parse failed"):
        return "parse"
    return _DEFAULT_STAGES.get(code, "unknown")


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    settle_delay_s: float = 0.8  # popup animation settle after a tap
    wake_settle_s: float = 1.5  # screen-on settle after KEYCODE_WAKEUP
    incidents_root: Path = incidents.DEFAULT_INCIDENTS_ROOT
    #: Retention budgets of ``incidents_root`` (oldest bundles pruned first,
    #: never the current run's) -- see :func:`bot2048.runner.incidents.prune_incidents`.
    incidents_max_bytes: int = incidents.DEFAULT_MAX_TOTAL_BYTES
    incidents_max_bundles: int = incidents.DEFAULT_MAX_BUNDLES
    #: Re-captures before a FATAL when a tap-less state (NOT_IN_GAME /
    #: UNKNOWN) is seen mid-game. One such frame is usually transient -- a
    #: mid-animation frame, the game-over card sliding in, a partial paw
    #: occlusion -- so the board momentarily drops below the GAME threshold.
    #: A GENUINELY stuck non-game screen (real game-over popup, bank home)
    #: stays non-GAME across every re-poll and still FATALs: blind taps stay
    #: forbidden (fail-closed). The budget is the SCREEN_UNEXPECTED row of the
    #: retry table (:data:`bot2048.runner.escalation.POLICIES`): 20 x 0.5 s
    #: outlasts the cat-mascot paw animation (LIVE incident 20260711T2040).
    transient_repolls: int = SCREEN_UNEXPECTED_REPOLLS
    transient_repoll_s: float = SCREEN_UNEXPECTED_REPOLL_S  # settle between re-polls


class Supervisor:
    """One ERROR recovery cycle + FATAL stop, wired to alerts/ops/incidents.

    Dependency-injected (``sleep``/``clock``/``classify``) so tests run with
    fake devices and zero real delays.
    """

    def __init__(
        self,
        device: SupervisorDevice,
        classify: Classifier,
        alerts: AlertWriter,
        config: SupervisorConfig | None = None,
        *,
        run_id: str | None = None,
        ops: OpsRepository | None = None,
        ring: incidents.MoveRing | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        auth_resolver: AuthResolver | None = None,
        incident_sink: incidents.IncidentSink | None = None,
    ) -> None:
        self.device = device
        self.classify = classify
        self.alerts = alerts
        self.config = config or SupervisorConfig()
        self.run_id = run_id
        self.ops = ops
        self.ring = ring
        #: Wired by the live runner (see :data:`AuthResolver`); left ``None``
        #: keeps the pre-existing GAME_OVER_UNEXPECTED policy on an auth wall.
        self.auth_resolver = auth_resolver
        self._sleep = sleep
        self._clock = clock
        #: THE incident path (docs/ops-monitoring.md §4): every ERROR/FATAL
        #: here goes through it. The live runner reads this attribute to share
        #: the same sink (one chain/dedupe state, one prune budget) for its own
        #: incidents and installs the fresh-capture hook / session context.
        self.incidents: incidents.IncidentSink = incident_sink or incidents.IncidentSink(
            root=self.config.incidents_root,
            alerts=alerts,
            ops=ops,
            run_id=run_id,
            ring=ring,
            max_total_bytes=self.config.incidents_max_bytes,
            max_bundles=self.config.incidents_max_bundles,
        )

    # -- public API -----------------------------------------------------------

    @property
    def last_incident_dir(self) -> str | None:
        """Bundle of the most recent incident (the loop links ``MOVE_ABORTED`` to it)."""
        return self.incidents.last_dir

    def handle_error(
        self,
        code: AlertCode,
        message: str,
        *,
        probe: ProbeResult | None = None,
        game_id: str | None = None,
        move_no: int | None = None,
        board_hex: str | None = None,
        attempt: int | None = None,
        extra: dict[str, Any] | None = None,
        stage: str | None = None,
        attempts: int | None = None,
    ) -> incidents.Image:
        """ERROR path (§5): bundle + ERROR alert + ONE recovery cycle.

        ``probe`` is reused from ``confirm_incident`` when available so the
        alert describes exactly what was seen; otherwise a fresh probe is
        taken. Returns a healthy GAME frame on success; raises
        :class:`FatalIncident` otherwise. ``fatal_immediately`` codes
        (AUTH_REQUIRED, CRASH, RESTART_LOOP) skip recovery -- single FATAL.

        ``stage`` names the move-pipeline stage that failed (``capture`` /
        ``parse`` / ``verify`` / ``swipe`` / ``auth`` / ``restart`` ...;
        inferred from the code when omitted) and ``attempts`` how many tries
        the caller spent before escalating (defaults to the retry table's
        ``max_attempts`` -- callers reach here only once the budget is gone).
        """
        if probe is None:
            probe = probe_screen(self.device, self.classify)
        code = self._upgrade_code(code, probe)
        stage = stage or _default_stage(code, message)
        policy = policy_for(code)
        if attempts is None:
            attempts = policy.max_attempts
        if policy.fatal_immediately:
            self.fatal(
                code,
                message,
                probe=probe,
                game_id=game_id,
                move_no=move_no,
                board_hex=board_hex,
                attempt=attempt,
                extra=extra,
                stage=stage,
                attempts=attempts,
            )

        self.incidents.record(
            code,
            AlertLevel.ERROR,
            message,
            probe=probe,
            game_id=game_id,
            move_no=move_no,
            board_hex=board_hex,
            attempt=attempt,
            attempts=attempts,
            retry_policy=policy.describe(),
            stage=stage,
            extra=extra,
        )

        image = self._recover_once(code, probe, game_id=game_id, move_no=move_no)
        self._ops_event_simple(
            "INFO", "recovered", f"{code.value} recovered", game_id=game_id, ply=move_no
        )
        log.info("supervisor.recovered", code=code.value)
        return image

    def start_new_game(self, *, after_win: bool = False) -> tuple[float, incidents.Image]:
        """Restart flow: «Нова гра» -> «Так, нова гра» (calibrated taps only).

        Returns ``(t0, frame)`` where ``t0 = time.monotonic()`` right after
        the confirm tap -- the zero of the in-game wall-clock timer (see
        module docs). Raises :class:`FatalIncident` when the flow does not
        land on a healthy GAME screen (§5: GAME_OVER_UNEXPECTED policy).

        AUTH-TOLERANT: an expired session token can route «Нова гра» through
        the bank's re-authorization wall. When that happens the flow delegates
        to :attr:`auth_resolver` (the live runner's opt-in auto-login) via
        :meth:`_resolve_auth` and re-verifies the fresh board -- a token expiry
        between games is seamless (no GAME_OVER_UNEXPECTED FATAL, no wasted
        restart budget). With no resolver / auto-login off / a persistent wall
        the resolver escalates AUTH_REQUIRED FATAL (fail-closed).

        WIN-AWARE (``after_win=True``): the caller has JUST positively
        identified the «Ви зібрали 2048!» win popup on a real frame
        (WIN_POPUP_2048 anchors on the APK stream) and finalized the win --
        but that screen is FLAG_SECURE: ``screencap`` returns 0 bytes and
        ``uiautomator dump`` yields no hierarchy (docs/device-ui-notes.md), so
        the probe here degrades to a blind hold state (SECURE_UNCONFIRMED /
        image-less UNKNOWN) that the generic policy would FATAL as
        GAME_OVER_UNEXPECTED (LIVE incident 20260711T210529Z, back then
        mislabeled ``device_lost``). With ``after_win`` set, that blind probe
        IS the expected win terminal: tap the calibrated «Нова гра» point
        (ui_map.WIN_NEW_GAME). This does not relax the zero-blind-tap
        doctrine -- the surface was positively identified as the game's win
        popup moments ago by the caller, which is exactly the identification
        the doctrine requires; without ``after_win`` the blind state still
        FATALs with zero taps. DEVICE_LOST is never excused: it requires the
        APK frame stream to be dead too, which a live win screen never is.
        """
        probe = probe_screen(self.device, self.classify)
        if probe.state is ScreenState.CONFIRM_NEW_GAME:
            # Popup already up (e.g. a previous attempt died between taps).
            t0 = self._tap_confirm_yes()
        elif after_win and self._blind_hold(probe):
            # FLAG_SECURE-blind probe right after the caller finalized a win:
            # this is the «Ви зібрали 2048!» screen itself (see docstring).
            log.info("supervisor.win_screen_blind", state=probe.state.value)
            self.device.tap(*ui_map.WIN_NEW_GAME)
            t0 = self._clock()
        elif probe.state in (ScreenState.WIN_POPUP_2048, ScreenState.GAME_OVER_POPUP):
            # «Ви зібрали 2048!» win popup / «Гру завершено» game-over popup:
            # both put a «Нова гра» button that starts a fresh game directly
            # (no confirmation step -- the finished game is already over). A
            # single calibrated point (ui_map.WIN_NEW_GAME) lands on that button
            # in the win popup AND in BOTH game-over layouts (short/tall); see
            # ui_map for why the point is the overlap of the three button rects.
            self.device.tap(*ui_map.WIN_NEW_GAME)
            t0 = self._clock()
        elif probe.state is ScreenState.GAME:
            self.device.tap(*ui_map.NEW_GAME)
            self._sleep(self.config.settle_delay_s)
            # A token expiry can route «Нова гра» straight to the auth wall:
            # clear it inline (auto-login) instead of FATALing (see _resolve_auth).
            after = self._resolve_auth(probe_screen(self.device, self.classify))
            if after.state is ScreenState.CONFIRM_NEW_GAME:
                t0 = self._tap_confirm_yes()
            elif after.state is ScreenState.GAME:
                # No confirmation asked (no game in progress to lose).
                t0 = self._clock()
            else:
                self.fatal(
                    AlertCode.GAME_OVER_UNEXPECTED,
                    f"restart flow: after 'Нова гра' tap got {after.state.value}",
                    probe=after,
                    stage="restart",
                )
        else:
            self.fatal(
                AlertCode.GAME_OVER_UNEXPECTED,
                f"restart flow: cannot start from state {probe.state.value}",
                probe=probe,
                stage="restart",
            )
        self._sleep(self.config.settle_delay_s)
        # The «Нова гра» tap above may have kicked the app into re-auth (expired
        # token); resolve it inline so a token expiry between games is seamless
        # rather than a wasted GAME_OVER_UNEXPECTED FATAL + restart-budget hit.
        verify = self._resolve_auth(probe_screen(self.device, self.classify))
        if after_win:
            # The win -> fresh-board transition can stay FLAG_SECURE-blind for
            # a beat after the tap (LIVE 20260711T210529Z: the verify probe
            # FATAL'd on it). Re-poll tap-lessly until the fresh board shows;
            # a screen that never resolves still FATALs below (fail-closed).
            for _ in range(WIN_BLIND_VERIFY_REPOLLS):
                if not self._blind_hold(verify):
                    break
                log.info("supervisor.win_transition_blind_repoll")
                self._sleep(self.config.settle_delay_s)
                verify = self._resolve_auth(probe_screen(self.device, self.classify))
        if not verify.healthy:
            self.fatal(
                AlertCode.GAME_OVER_UNEXPECTED,
                f"restart flow: expected fresh GAME, got {verify.state.value} "
                f"(board_match_frac={verify.board_match_frac:.2f})",
                probe=verify,
                stage="restart",
                attempts=(WIN_BLIND_VERIFY_REPOLLS + 1) if after_win else 1,
            )
        assert verify.image is not None
        if self.ring is not None:
            self.ring.set_good_frame(verify.image)
        log.info("supervisor.new_game_started")
        return t0, verify.image

    def _resolve_auth(self, probe: ProbeResult) -> ProbeResult:
        """Clear an AUTH_REQUIRED wall met mid-restart, then re-probe.

        The restart flow can route through the bank's re-authorization wall
        when the session token expired (measured live: «Нова гра» -> auth), so
        a natural game-over restart must NOT FATAL on it. With an
        :attr:`auth_resolver` wired (the live runner's opt-in auto-login) the
        wall is cleared and the screen re-probed so the caller sees the fresh
        board; the resolver raises :class:`FatalIncident` itself when it may
        not clear it (auto-login off / no PIN / attempts exhausted -- fail
        closed). Without a resolver, or on any non-auth state, the probe is
        returned unchanged so the existing GAME_OVER_UNEXPECTED policy holds.
        """
        if probe.state is not ScreenState.AUTH_REQUIRED or self.auth_resolver is None:
            return probe
        log.info("supervisor.restart_auth_wall")
        # Returns True (wall cleared -> re-probe) or raises FatalIncident.
        if not self.auth_resolver(probe):
            return probe  # declined without raising: keep the fail-closed FATAL
        return probe_screen(self.device, self.classify)

    def fatal(
        self,
        code: AlertCode,
        message: str,
        *,
        probe: ProbeResult | None = None,
        game_id: str | None = None,
        move_no: int | None = None,
        board_hex: str | None = None,
        attempt: int | None = None,
        extra: dict[str, Any] | None = None,
        traceback_text: str | None = None,
        stage: str | None = None,
        attempts: int | None = None,
    ) -> NoReturn:
        """FATAL stop (§5): bundle + FATAL alert, then raise. NO taps.

        A FATAL right after an ERROR of the same code (the failed recovery
        cycle) is CHAINED into the ERROR's bundle by the sink -- one incident,
        one folder (``alert_fatal.json`` / ``screenshot_fatal.png``).
        """
        policy = policy_for(code)
        record = self.incidents.record(
            code,
            AlertLevel.FATAL,
            message,
            probe=probe,
            game_id=game_id,
            move_no=move_no,
            board_hex=board_hex,
            attempt=attempt,
            attempts=attempts if attempts is not None else policy.max_attempts,
            retry_policy=policy.describe(),
            stage=stage or _default_stage(code, message),
            extra=extra,
            traceback_text=traceback_text,
        )
        log.error("supervisor.fatal", code=code.value, message=message)
        raise FatalIncident(record.alert)

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _blind_hold(probe: ProbeResult) -> bool:
        """True when the probe is the FLAG_SECURE blind-screen signature.

        ``probe_screen`` degrades a 0-byte screencap into SECURE_UNCONFIRMED
        (all ui dumps blank/failed) or an image-less UNKNOWN (a dump came back
        but carried no auth anchor). Both mean "cannot see the screen, device
        answered" -- on the win screen that IS the expected surface. A
        classifier UNKNOWN on a real frame (``image`` present) is NOT blind,
        and DEVICE_LOST / AUTH_REQUIRED are never excused here.
        """
        return probe.image is None and probe.state in (
            ScreenState.SECURE_UNCONFIRMED,
            ScreenState.UNKNOWN,
        )

    @staticmethod
    def _upgrade_code(code: AlertCode, probe: ProbeResult) -> AlertCode:
        if probe.state is ScreenState.AUTH_REQUIRED:
            return AlertCode.AUTH_REQUIRED
        if probe.state is ScreenState.DEVICE_LOST:
            return AlertCode.DEVICE_LOST
        return code

    def _tap_confirm_yes(self) -> float:
        self.device.tap(*ui_map.CONFIRM_YES)
        return self._clock()  # game timer zero: wall-clock from this instant

    def _recover_once(
        self,
        code: AlertCode,
        probe: ProbeResult,
        *,
        game_id: str | None,
        move_no: int | None,
    ) -> incidents.Image:
        """Exactly one recovery cycle; returns a healthy frame or raises."""
        if probe.healthy:
            assert probe.image is not None
            return probe.image  # already fine (transient glitch)

        if probe.state is ScreenState.SCREEN_OFF:
            self.device.wake()  # keyevent, not an in-app tap
            self._sleep(self.config.wake_settle_s)
        elif probe.state is ScreenState.CONFIRM_NEW_GAME:
            # Never confirm a reset during recovery -- keep the current game.
            self.device.tap(*ui_map.CONTINUE_GAME)
            self._sleep(self.config.settle_delay_s)
        elif probe.state in (ScreenState.NOT_IN_GAME, ScreenState.UNKNOWN):
            # No calibrated tap is safe here, but a lone NOT_IN_GAME/UNKNOWN
            # frame mid-game is usually a transient (animation / paw occlusion
            # / a popup mid-slide) that momentarily drops the board below the
            # GAME threshold. Re-poll before ending the run; a persistent
            # non-game screen still FATALs (fail-closed, no blind taps).
            return self._repoll_or_fatal(code, probe, game_id=game_id, move_no=move_no)
        else:
            # SCREEN_OFF was handled above; any remaining non-actionable state
            # (DEVICE_LOST, AUTH_REQUIRED, ...) is not a transient board dip --
            # go straight to FATAL, exactly as before.
            self.fatal(
                code,
                f"no safe recovery action in state {probe.state.value} "
                "(blind taps forbidden)",
                probe=probe,
                game_id=game_id,
                move_no=move_no,
                stage="recovery",
                attempts=1,
            )

        verify = probe_screen(self.device, self.classify)
        if verify.healthy:
            assert verify.image is not None
            if self.ring is not None:
                self.ring.set_good_frame(verify.image)
            return verify.image
        self.fatal(
            code,
            f"recovery cycle failed: still {verify.state.value} "
            f"(board_match_frac={verify.board_match_frac:.2f})",
            probe=verify,
            game_id=game_id,
            move_no=move_no,
            stage="recovery",
            attempts=1,
        )

    def _repoll_or_fatal(
        self,
        code: AlertCode,
        probe: ProbeResult,
        *,
        game_id: str | None,
        move_no: int | None,
    ) -> incidents.Image:
        """Re-capture up to ``transient_repolls`` times looking for GAME.

        Tolerates a single occluded/animating/transition frame (a re-poll
        recovers) while staying fail-closed: if every re-poll still shows a
        non-GAME screen, no safe tap exists and the run FATALs. ``probe`` is
        the first (already classified) non-recoverable observation; the most
        recent probe is what the FATAL bundle reports if the streak holds.
        """
        latest = probe
        for attempt in range(1, self.config.transient_repolls + 1):
            self._sleep(self.config.transient_repoll_s)
            latest = probe_screen(self.device, self.classify)
            if latest.healthy:
                assert latest.image is not None
                if self.ring is not None:
                    self.ring.set_good_frame(latest.image)
                log.info(
                    "supervisor.transient_recovered",
                    code=code.value,
                    first_state=probe.state.value,
                    attempt=attempt,
                )
                return latest.image
            log.info(
                "supervisor.transient_repoll",
                code=code.value,
                state=latest.state.value,
                board_match_frac=round(latest.board_match_frac, 3),
                attempt=attempt,
                of=self.config.transient_repolls,
            )
        self.fatal(
            code,
            f"no safe recovery action in state {latest.state.value} "
            f"(blind taps forbidden); {self.config.transient_repolls} "
            "re-polls all non-GAME",
            probe=latest,
            game_id=game_id,
            move_no=move_no,
            stage="recovery",
            attempts=self.config.transient_repolls + 1,
        )

    def _ops_event_simple(
        self,
        level: str,
        code: str,
        message: str,
        *,
        game_id: str | None,
        ply: int | None,
    ) -> None:
        if self.ops is None or self.run_id is None:
            return
        try:
            self.ops.record_event(
                self.run_id, level, code, message, game_id=game_id, ply=ply
            )
        except StorageError as exc:
            log.warning("supervisor.ops_event_failed", error=str(exc))

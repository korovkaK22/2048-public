"""Auth-wall handling for the live loop (R5, docs/archive/refactor-plan.md).

The hardened auth surface of the live pipeline: :class:`AuthFlow` owns the
opt-in auto-login policy for the AUTH_REQUIRED wall, both on the main play loop
(:meth:`handle_auth`, wired into :class:`~bot2048.runner.observation.ObservationGate`
as its ``auth_handler``) and during the restart flow
(:meth:`resolve_restart_auth`, wired into ``Supervisor.auth_resolver``) and the
start-of-session reset (#46, :meth:`resolve_reset_auth`, handed to
:func:`~bot2048.runner.newgame.reset_to_new_game` as ``resolve_auth`` --
best-effort, never FATAL).
:class:`~bot2048.runner.live.LiveRunner` holds one and delegates to it. The
extraction is behavior-preserving.

SECURITY CONTRACT (do NOT weaken -- carried verbatim from the original
``LiveRunner`` methods; see :mod:`bot2048.device.auth`):

* the PIN is read ONLY from the injected ``pin_reader`` (default
  ``device.auth.read_pin``, env ``MONO_PIN``); it never touches a log line, an
  argument, config, the DB or an incident file;
* taps are issued only at the calibrated pin-pad points via
  :func:`~bot2048.device.auth.enter_pin` -- no exploration on the auth screen;
* auto-login is OPT-IN: with it OFF (or no PIN) the wall escalates the
  AUTH_REQUIRED FATAL and a human unlocks the phone (zero taps).

Two collaborations stay in :class:`~bot2048.runner.live.LiveRunner` and are
handed in as callbacks so no recording / gate logic leaks into this module:

* ``parse_or_escalate`` -- the gate's board parse (a NEW auth-cleared frame
  decides resume-vs-reset);
* ``on_game_interrupted`` -- finalization of a game the app regenerated
  (``_end_game(state, "interrupted")``); this module never records.

``clock`` / ``sleep`` are thin accessors of the runner's CURRENT attributes
(not bound once), so a test that swaps ``runner._clock`` / ``runner._sleep``
after construction is honored exactly as before the extraction.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

import structlog

from bot2048.core.types import Board
from bot2048.device.auth import enter_pin
from bot2048.runner.alerts import AlertCode
from bot2048.runner.errors import LiveLoopError
from bot2048.runner.escalation import ProbeResult, probe_screen
from bot2048.runner.incidents import Image, MoveRing
from bot2048.runner.live_config import LiveConfig
from bot2048.runner.outcomes import _GameOverPopup, _GameReset, _WinPopup2048
from bot2048.vision.states import ScreenState

if TYPE_CHECKING:
    from bot2048.runner.live import (
        GameSupervisor,
        LiveDevice,
        PinReader,
        StateDetector,
        _GameState,
    )

log = structlog.get_logger(__name__)


class AuthFlow:
    """Opt-in auto-login for the AUTH_REQUIRED wall (main loop + restart flow).

    All collaborators are injected by :class:`~bot2048.runner.live.LiveRunner`.
    The loop-guard counter for a terminal popup surfacing UNDER the auth wall
    (:attr:`_auth_popup_restarts`) lives here now; :meth:`note_move_committed`
    resets it when a real move progresses (called from ``_commit_move``).
    """

    def __init__(
        self,
        *,
        device: LiveDevice,
        detect: StateDetector,
        config: LiveConfig,
        pin_reader: PinReader,
        supervisor: GameSupervisor,
        capture_lock: AbstractContextManager[bool],
        clock: Callable[[], float],
        sleep: Callable[[float], Any],
        parse_or_escalate: Callable[[Image, _GameState | None], Board],
        on_game_interrupted: Callable[[_GameState], object],
        notify: Callable[[str, str, str], None],
        ops_info: Callable[[str, str, str | None, int | None], None],
        ring: MoveRing | None = None,
    ) -> None:
        self.device = device
        self.detect = detect
        self.config = config
        self._pin_reader = pin_reader
        self.supervisor = supervisor
        self._capture_lock = capture_lock
        self._clock = clock
        self._sleep = sleep
        self._parse_or_escalate = parse_or_escalate
        self._on_game_interrupted = on_game_interrupted
        self._notify = notify
        self._ops_info = ops_info
        self.ring = ring
        # Loop-guard for a terminal popup (win / game-over) surfacing UNDER the
        # auth wall: each such auth->popup restart bumps this, a committed move
        # resets it. Back-to-back popups with no move between them (nothing ever
        # progresses) trip a fail-closed FATAL at auth_popup_restart_limit
        # instead of spinning forever (see handle_auth).
        self._auth_popup_restarts = 0

    def note_move_committed(self) -> None:
        """A move committed on a healthy board: clear the popup loop-guard.

        Genuine progress means any prior auth->terminal-popup restart streak did
        NOT stall, so only BACK-TO-BACK popup restarts with no move between them
        (the true stuck loop) ever reach the fail-closed FATAL in
        :meth:`handle_auth`.
        """
        self._auth_popup_restarts = 0

    def handle_auth(self, state: _GameState | None, probe: ProbeResult) -> Image:
        """Auth wall: input stops NOW; opt-in auto-login or supervisor FATAL.

        Returns a healthy GAME frame only when auto-login succeeded AND the
        game survived (identical board = mere lock -> resume). A regenerated
        game closes the current one as ``interrupted`` (via the injected
        ``on_game_interrupted`` callback) and raises :class:`_GameReset`.

        A KNOWN terminal popup revealed once the PIN clears the wall
        (:data:`ScreenState.WIN_POPUP_2048` / :data:`ScreenState.GAME_OVER_POPUP`
        -- e.g. auto-login typed the PIN over a «Ви зібрали 2048!» card) is NOT
        a FATAL: it is surfaced as the SAME popup exception the in-move capture
        raises, so every caller funnels it into the ONE auth-tolerant restart
        path (``_acquire_initial_game`` -> ``supervisor.start_new_game`` at
        startup, ``_play_game`` -> ``_finish_win``/``_finish_game_over`` ->
        restart mid-game) -- never a blind FATAL. A loop-guard
        (:attr:`LiveConfig.auth_popup_restart_limit`) bounds a
        popup->restart->popup cycle that never progresses (fail-closed).
        Everything else (unknown / secure / no PIN) is the unchanged FATAL.
        """
        log.warning("live.auth_wall", auto_login=self.config.auto_login)
        pin = self._pin_reader() if self.config.auto_login else None
        if pin is None:
            reason = (
                "auto-login disabled" if not self.config.auto_login else "MONO_PIN not set"
            )
            # AUTH_REQUIRED is fatal_immediately -> this call never returns.
            self.supervisor.handle_error(
                AlertCode.AUTH_REQUIRED,
                f"auth wall and {reason} -- pausing for a human (no taps)",
                probe=probe,
                game_id=state.game_id if state is not None else None,
                move_no=state.ply if state is not None else None,
            )
            raise LiveLoopError("AUTH_REQUIRED policy must be fatal")  # pragma: no cover
        # A post-idle auth wall is EXPECTED on an unattended run, and a single
        # flaky PIN entry (a mistimed tap, an animation still settling) must not
        # end the record run. Route the PIN through the SAME bounded loop the
        # restart/reset delegates use: up to ``auth_retries`` attempts, re-probing
        # after each, honoring the configured cap (lockout-safe: never more than
        # ``auth_retries`` entries) and never logging/storing the PIN. Only when
        # the wall survives EVERY attempt does the fail-closed FATAL below fire.
        after = self._pin_attempts(
            pin,
            probe,
            cleared_event="live.auth_cleared",
            retry_event="live.auth_retry",
        )
        if after.state in (ScreenState.WIN_POPUP_2048, ScreenState.GAME_OVER_POPUP):
            # A known, button-bearing terminal popup was hiding behind the auth
            # wall (auto-login typed the PIN over it). This is the incident that
            # used to FATAL "auto-login did not reach GAME": it is NOT a
            # dangerous unknown screen, so surface it as the SAME popup exception
            # the in-move capture (_capture_game_frame) raises. Every caller
            # already funnels that into the auth-tolerant restart path
            # (start_new_game): _acquire_initial_game at startup (state is None),
            # _play_game mid-game (state is not None). Reuse, never duplicate.
            self._auth_popup_restarts += 1
            log.warning(
                "live.auth_terminal_popup",
                state=after.state.value,
                restarts=self._auth_popup_restarts,
                limit=self.config.auth_popup_restart_limit,
            )
            if self._auth_popup_restarts > self.config.auth_popup_restart_limit:
                # Nothing has progressed (no committed move resets the counter)
                # across this many popup->restart cycles: fail closed instead of
                # looping forever. This is the only path here that FATALs on a
                # known popup -- a genuinely stuck restart loop.
                self.supervisor.fatal(
                    AlertCode.SCREEN_UNEXPECTED,
                    f"terminal popup under the auth wall {self._auth_popup_restarts}x "
                    f"in a row (> auth_popup_restart_limit="
                    f"{self.config.auth_popup_restart_limit}) with no move between "
                    f"restarts -- failing closed (last: {after.state.value}: {after.reason})",
                    probe=after,
                    game_id=state.game_id if state is not None else None,
                    move_no=state.ply if state is not None else None,
                )
            if after.state is ScreenState.WIN_POPUP_2048:
                raise _WinPopup2048(after.reason)
            raise _GameOverPopup(after.reason)
        if not after.healthy:
            self.supervisor.handle_error(
                AlertCode.AUTH_REQUIRED,
                f"auto-login did not reach GAME (got {after.state.value}: {after.reason})",
                probe=after,
                game_id=state.game_id if state is not None else None,
                move_no=state.ply if state is not None else None,
            )
            raise LiveLoopError("AUTH_REQUIRED policy must be fatal")  # pragma: no cover
        assert after.image is not None
        if self.ring is not None:
            self.ring.set_good_frame(after.image)
        if state is None:
            return after.image  # session start: adopt whatever game is there
        board = self._parse_or_escalate(after.image, state)
        if board == state.board:
            # Mere idle/screen-lock: the game SURVIVES (verified live).
            self._ops_info("RESUMED", "auth wall passed, game intact", state.game_id, state.ply)
            self._notify("INFO", "RESUMED", "▶️ ПІН введено — гра вціліла, продовжую")
            log.info("live.resumed_after_auth", game_id=state.game_id)
            return after.image
        # Any other board: the process was killed, the app made a NEW game.
        # Close the old one; NEVER try to finish the old board (docs/edge-cases.md).
        self._on_game_interrupted(state)
        self._notify("INFO", "RESTARTED", "🔁 Після ПІН — застосунок створив нову гру")
        raise _GameReset(board=board, t0=self._clock())

    def resolve_restart_auth(self, probe: ProbeResult) -> bool:
        """Supervisor delegate: clear an auth wall hit DURING the restart flow.

        The bank re-routes «Нова гра» through re-authorization when the session
        token expired (measured live), so a natural game-over restart can land
        on the PIN pad instead of a fresh board. This mirrors the main loop's
        :meth:`handle_auth` opt-in policy but WITHOUT spending a restart-budget
        slot on a FATAL: with auto-login + a PIN we type it (up to
        :attr:`LiveConfig.auth_retries` tries) and return ``True`` so the
        supervisor re-verifies the fresh board; with auto-login off / no PIN /
        the wall persisting we escalate the SAME AUTH_REQUIRED FATAL as before
        (fail-closed, no blind taps). Reuses :func:`enter_pin` -- the PIN-entry
        logic is not duplicated.
        """
        log.warning("live.restart_auth_wall", auto_login=self.config.auto_login)
        pin = self._pin_reader() if self.config.auto_login else None
        if pin is None:
            reason = (
                "auto-login disabled" if not self.config.auto_login else "MONO_PIN not set"
            )
            # AUTH_REQUIRED is fatal_immediately -> handle_error never returns.
            self.supervisor.handle_error(
                AlertCode.AUTH_REQUIRED,
                f"restart flow hit the auth wall and {reason} -- pausing for a human (no taps)",
                probe=probe,
            )
            raise LiveLoopError("AUTH_REQUIRED policy must be fatal")  # pragma: no cover
        self._notify("INFO", "AUTH", "🔐 Токен протух на рестарті — вводжу ПІН")
        after = self._pin_attempts(
            pin,
            probe,
            cleared_event="live.restart_auth_cleared",
            retry_event="live.restart_auth_retry",
        )
        if after.state is not ScreenState.AUTH_REQUIRED:
            return True
        # The wall survived every attempt: fail closed exactly as doctrine.
        self.supervisor.handle_error(
            AlertCode.AUTH_REQUIRED,
            f"restart auto-login did not clear the wall after "
            f"{self.config.auth_retries} attempt(s) (still {after.state.value})",
            probe=after,
        )
        raise LiveLoopError("AUTH_REQUIRED policy must be fatal")  # pragma: no cover

    def resolve_reset_auth(self, probe: ProbeResult) -> bool:
        """Reset-on-start delegate (#46): clear an auth wall met by the
        session-start «Нова гра» reset -- best-effort, NEVER fatal.

        Same token-expiry re-route as :meth:`resolve_restart_auth` and the same
        SECURITY CONTRACT (PIN only via ``pin_reader``, taps only at the
        calibrated pad points via :func:`enter_pin`, auto-login strictly
        opt-in), but the start-of-session reset is optional polish, so no
        ``handle_error`` here: with auto-login off, no PIN, or the wall
        persisting this returns ``False`` (zero further taps) and the caller
        skips the reset -- the session continues, and if the wall is real the
        normal acquire path meets it next and applies the unchanged
        AUTH_REQUIRED doctrine. The reset must never turn into a FATAL.
        """
        log.warning("live.reset_auth_wall", auto_login=self.config.auto_login)
        pin = self._pin_reader() if self.config.auto_login else None
        if pin is None:
            reason = (
                "auto-login disabled" if not self.config.auto_login else "MONO_PIN not set"
            )
            log.warning("live.reset_auth_unresolved", reason=reason)
            return False
        self._notify("INFO", "AUTH", "🔐 Стіна авторизації на старті — вводжу ПІН")
        after = self._pin_attempts(
            pin,
            probe,
            cleared_event="live.reset_auth_cleared",
            retry_event="live.reset_auth_retry",
        )
        if after.state is not ScreenState.AUTH_REQUIRED:
            return True
        log.warning(
            "live.reset_auth_unresolved",
            reason=f"wall persisted after {self.config.auth_retries} attempt(s)",
        )
        return False

    def _pin_attempts(
        self,
        pin: str,
        probe: ProbeResult,
        *,
        cleared_event: str,
        retry_event: str,
    ) -> ProbeResult:
        """Shared PIN loop of the restart / reset auth delegates.

        Types the PIN via :func:`enter_pin` (calibrated pad taps only) up to
        :attr:`LiveConfig.auth_retries` times, re-probing after each entry, and
        returns the first probe that is no longer AUTH_REQUIRED -- or the last
        still-walled probe once retries are exhausted. Deciding what a
        persisting wall MEANS (restart: the FATAL doctrine; reset: skip and
        continue) stays with the caller.
        """
        after = probe
        for attempt in range(1, self.config.auth_retries + 1):
            enter_pin(self.device, pin, sleep=self._sleep)
            self._sleep(self.config.auth_settle_s)
            with self._capture_lock:
                after = probe_screen(self.device, self.detect)
            if after.state is not ScreenState.AUTH_REQUIRED:
                log.info(cleared_event, attempt=attempt)
                return after
            log.warning(retry_event, attempt=attempt, of=self.config.auth_retries)
        return after

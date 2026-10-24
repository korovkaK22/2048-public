"""Never-crash outer-loop recovery for the live session (R6, docs/archive/refactor-plan.md).

The overnight-hardening policy that keeps a session alive: :class:`SessionRecovery`
decides which failures are transient (:meth:`recoverable`) and, for those,
recovers the session in place (:meth:`recover`: incident bundle via the shared
:class:`~bot2048.runner.incidents.IncidentSink` -- fresh screenshot, traceback,
``live_events`` row -- + ERROR alert + reconnect + backoff) under a sliding-hour
budget.
:class:`~bot2048.runner.live.LiveRunner` holds one and calls it from the game
loop; the extraction is behavior-preserving.

POLICY (do NOT change -- this is what keeps overnight runs alive):

* ``AUTH_REQUIRED`` is NON-recoverable (a human must unlock the phone), as are
  ``CRASH`` / ``RESTART_LOOP`` and every :class:`LiveLoopError` (an internal
  contract violation is a CRASH, never a retry); only the transient FATAL codes
  in :attr:`_RECOVERABLE_FATAL_CODES` (adb drop, a restart flow that missed the
  popup) and plain :class:`Exception` s are recovered;
* the retry budget is a SLIDING one-hour window
  (:attr:`LiveConfig.recover_max_per_hour`, mirroring the lifecycle restart
  budget); when it is exhausted the original exception is re-raised unchanged and
  the ordinary FATAL/CRASH path takes over;
* recovery with the budget disabled (``recover_max_per_hour <= 0``) is OFF --
  the exact pre-hardening behavior.

``clock`` / ``sleep`` are thin accessors of the runner's CURRENT attributes (not
bound once), so a test that swaps ``runner._clock`` / ``runner._sleep`` after
construction is honored exactly as before the extraction. The recovery-timestamp
deque is owned here and exposed to :class:`~bot2048.runner.live.LiveRunner` via
:attr:`recoveries` (the ``LiveRunner._recoveries`` view the tests read).
"""

from __future__ import annotations

import traceback
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

# NOTE: module import, not a from-import (mirrors runner/live.py): device/
# supervisor.py imports bot2048.runner.incidents, so binding FatalIncident at
# import time can crash on the half-initialized module when the device layer
# loads first. Reference ``_supervisor_mod.FatalIncident`` lazily instead.
from bot2048.device import supervisor as _supervisor_mod
from bot2048.runner.alerts import AlertCode, AlertLevel, AlertWriter
from bot2048.runner.errors import LiveLoopError
from bot2048.runner.escalation import policy_for
from bot2048.runner.incidents import IncidentSink, MoveRing
from bot2048.runner.live_config import LiveConfig

if TYPE_CHECKING:
    from bot2048.runner.live import HeartbeatSink

log = structlog.get_logger(__name__)


class SessionRecovery:
    """Never-crash outer-loop policy + recovery for one live session.

    All collaborators are injected by :class:`~bot2048.runner.live.LiveRunner`.
    """

    #: FATAL codes the outer loop may retry: transient by nature (adb drop,
    #: a restart flow that missed the popup). AUTH_REQUIRED / CRASH /
    #: RESTART_LOOP and persistent-screen FATALs stay fatal (doctrine).
    _RECOVERABLE_FATAL_CODES = frozenset(
        {AlertCode.DEVICE_LOST, AlertCode.GAME_OVER_UNEXPECTED}
    )

    def __init__(
        self,
        *,
        config: LiveConfig,
        clock: Callable[[], float],
        sleep: Callable[[float], Any],
        alerts: AlertWriter,
        incidents: IncidentSink,
        run_id: str | None,
        notify: Callable[[str, str, str], None],
        reconnect: Callable[[], Any] | None = None,
        heartbeat: HeartbeatSink | None = None,
        ring: MoveRing | None = None,
    ) -> None:
        self.config = config
        self._clock = clock
        self._sleep = sleep
        self.alerts = alerts
        self.incidents = incidents
        self.run_id = run_id
        self._notify = notify
        self.reconnect = reconnect
        self.heartbeat = heartbeat
        self.ring = ring
        self._recoveries: deque[float] = deque()  # session-recovery timestamps

    @property
    def recoveries(self) -> deque[float]:
        """The sliding-window recovery timestamps (the ``_recoveries`` view)."""
        return self._recoveries

    def recoverable(self, exc: BaseException, games_done: int) -> bool:
        """Outer-loop policy: recover transient trouble, propagate the rest."""
        if self.config.recover_max_per_hour <= 0:
            return False  # recovery disabled: pre-hardening behavior
        if self.config.max_games and games_done >= self.config.max_games:
            return False  # the session is over anyway; let the cause escalate
        if isinstance(exc, _supervisor_mod.FatalIncident):
            return exc.alert.code in self._RECOVERABLE_FATAL_CODES
        if isinstance(exc, LiveLoopError):
            return False  # internal contract violation: CRASH, never recover
        # KeyboardInterrupt / SystemExit are BaseException-only: propagate.
        return isinstance(exc, Exception)

    def recover(self, exc: BaseException) -> None:
        """Recover the SESSION after ``exc``: incident + alert + reconnect.

        A sliding-hour budget (:attr:`LiveConfig.recover_max_per_hour`,
        mirroring the lifecycle restart budget) bounds the retries; when it
        is exhausted ``exc`` is re-raised unchanged and the ordinary
        FATAL/CRASH path takes over.
        """
        now = self._clock()
        self._recoveries.append(now)
        while self._recoveries and self._recoveries[0] < now - 3600.0:
            self._recoveries.popleft()
        if len(self._recoveries) > self.config.recover_max_per_hour:
            log.error(
                "live.recover_budget_exhausted",
                recoveries=len(self._recoveries),
                per_hour=self.config.recover_max_per_hour,
            )
            raise exc
        # ONE incident path (docs/ops-monitoring.md §4): a FatalIncident already
        # has its bundle on disk -> the LOOP_ERROR alert ATTACHES to it (same
        # folder, one more alert line); any other exception gets a full bundle
        # with a FRESH screenshot + traceback + last moves, and in both cases a
        # live_events row with incident_dir set.
        attach_to = (
            exc.alert.incident_dir
            if isinstance(exc, _supervisor_mod.FatalIncident)
            else None
        )
        policy = policy_for(AlertCode.LOOP_ERROR)
        try:
            self.incidents.record(
                AlertCode.LOOP_ERROR,
                AlertLevel.ERROR,
                f"session recovering from {type(exc).__name__}: {exc}",
                attempt=len(self._recoveries),
                attempts=len(self._recoveries),
                retry_policy=policy.describe(),
                stage="session",
                extra={"cause": type(exc).__name__},
                traceback_text=(
                    "".join(traceback.format_exception(exc)) if attach_to is None else None
                ),
                attach_to=attach_to,
            )
        except Exception as bexc:  # incident trouble must not stop recovery
            log.warning("live.recover_bundle_failed", error=str(bexc))
        log.warning(
            "live.session_recovering",
            cause=type(exc).__name__,
            attempt=len(self._recoveries),
            per_hour=self.config.recover_max_per_hour,
        )
        self._notify(
            "INFO",
            "RECOVERING",
            f"♻️ Відновлення після {type(exc).__name__} — сесія триває",
        )
        if self.reconnect is not None:  # e.g. AdbDevice.connect
            try:
                self.reconnect()
            except Exception as rexc:
                log.warning("live.reconnect_failed", error=str(rexc))
        if self.heartbeat is not None:
            self.heartbeat.update()  # the heartbeat thread keeps beating regardless
        self._sleep(self.config.recover_backoff_s)

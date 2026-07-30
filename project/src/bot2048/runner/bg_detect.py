"""Off-loop periodic full-frame screen detection for the APK live path.

In APK mode (``device/apk_capture.py``, option D) the play loop reads each
move's board from 22-byte packets (~one socket round trip) and never blocks on
vision. But the packet cannot see popups / the CONFIRM dialog / the
``FLAG_SECURE`` auth wall, so ``detect_state`` must still run on a periodic full
``screencap`` -- and over WiFi that screencap costs ~0.65 s. Run INLINE (the
old ``LiveRunner._apk_control_tick``) it froze the visible board once every
~10 s.

:class:`BackgroundStateProbe` moves that screencap+classify onto its own daemon
thread. It probes every ``interval_s`` and, whenever it sees a NON-healthy
screen, publishes the latest :class:`~bot2048.runner.escalation.ProbeResult` as
a pending "alert" under a lock. The play loop consumes it cheaply once per move
via :meth:`poll_alert`; on a hit it re-confirms + handles the screen fail-closed
on the MAIN thread (the authoritative popup/auth/recovery handling never leaves
the loop -- the worker only ever RAISES suspicion, it never suppresses it).

Fail-closed by construction:

* a healthy probe publishes nothing, so the loop's per-move check is free;
* the worker is only a TRIGGER -- the main thread re-observes and decides, so a
  stale alert (a popup that already cleared) simply re-confirms as healthy;
* if the worker is silent, stuck, or dead the loop is no worse than before --
  the packet's own not-in-game / secure flags stay an immediate in-loop hint,
  and the runner falls back to its synchronous periodic tick when the worker
  is not :attr:`is_running`.

The thread is a daemon and every stop path is bounded (:meth:`close` joins with
a timeout), so a probe blocked on a dead adb link can never wedge shutdown.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self

import structlog

from bot2048.runner.escalation import ProbeResult

__all__ = ["BackgroundStateProbe", "StateProbe"]

log = structlog.get_logger(__name__)

#: Bound on :meth:`BackgroundStateProbe.close` join -- long enough to let a
#: single in-flight ``screencap`` (~0.65 s over WiFi) return, short enough that
#: a genuinely wedged probe never stalls session shutdown (the thread is a
#: daemon, so it dies with the process regardless).
DEFAULT_JOIN_TIMEOUT_S: float = 5.0


class StateProbe(Protocol):
    """The off-loop detect surface the live loop depends on (injectable).

    :class:`BackgroundStateProbe` is the production implementation; tests inject
    a scripted fake so the loop's consume-and-confirm policy is exercised
    deterministically without a real thread.
    """

    def start(self) -> None: ...

    def poll_alert(self) -> ProbeResult | None:
        """The latest NON-healthy observation since the last call, else ``None``.

        Consuming semantics: a pending alert is returned exactly once (cleared
        on read). A healthy screen never produces one.
        """
        ...

    def note_active(self, active: bool) -> None:
        """Enable/disable probing (``False`` = stand down and drop pending)."""
        ...

    @property
    def is_running(self) -> bool:
        """True while the worker thread is alive and can publish alerts."""
        ...

    def close(self) -> None:
        """Stop the worker and join it (bounded; never raises)."""
        ...


class BackgroundStateProbe:
    """Daemon-thread periodic ``screencap`` + ``detect_state``; see module docs.

    ``probe`` is the one observation the worker runs -- in production a closure
    over :func:`~bot2048.runner.escalation.probe_screen` bound to the raw device
    and detector, serialized against the main loop's own full-frame captures by
    the runner's capture lock. It must never raise (``probe_screen`` degrades
    device trouble into a ``DEVICE_LOST`` / ``SECURE_UNCONFIRMED`` / ``UNKNOWN``
    result -- a FLAG_SECURE-blind screencap with APK frames still flowing is
    the non-tap ``SECURE_UNCONFIRMED``, never a false ``DEVICE_LOST``); any
    exception that does escape is swallowed so a single hiccup never kills the
    worker.
    """

    def __init__(
        self,
        probe: Callable[[], ProbeResult],
        *,
        interval_s: float = 10.0,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        name: str = "bot2048-bg-detect",
    ) -> None:
        if interval_s < 0:
            raise ValueError(f"interval_s must be >= 0, got {interval_s}")
        if join_timeout_s < 0:
            raise ValueError(f"join_timeout_s must be >= 0, got {join_timeout_s}")
        self._probe = probe
        self._interval_s = interval_s
        self._join_timeout_s = join_timeout_s
        self._name = name
        self._lock = threading.Lock()
        self._pending: ProbeResult | None = None
        self._active = threading.Event()
        self._active.set()  # probing on by default
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Spawn the daemon worker (idempotent)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._active.set()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        log.info("bg_detect.started", interval_s=self._interval_s)

    def close(self) -> None:
        """Signal stop and join the worker; bounded, best-effort, never raises.

        A worker parked between probes exits immediately; one blocked mid-probe
        (a hung ``screencap``) is waited on for at most ``join_timeout_s`` and
        then abandoned to the daemon reaper -- shutdown never hangs.
        """
        self._stop.set()
        self._active.set()  # wake a stood-down worker so it can see the stop
        thread, self._thread = self._thread, None
        if thread is None:
            return
        thread.join(self._join_timeout_s)
        if thread.is_alive():
            log.warning("bg_detect.join_timeout", timeout_s=self._join_timeout_s)
        else:
            log.info("bg_detect.stopped")

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- publish / consume --------------------------------------------------

    def poll_alert(self) -> ProbeResult | None:
        with self._lock:
            pending, self._pending = self._pending, None
        return pending

    def note_active(self, active: bool) -> None:
        # Toggle the event first, then drop any pending under the lock. A probe
        # that was already in flight re-checks the flag under the SAME lock
        # before publishing (see :meth:`_run`), so a stand-down can never leave
        # a stale alert behind -- whichever of the two runs last wins, and both
        # orderings end with no pending alert while stood down.
        if active:
            self._active.set()
        else:
            self._active.clear()
        with self._lock:
            self._pending = None

    # -- worker -------------------------------------------------------------

    def _run(self) -> None:
        # `_stop.wait(interval)` doubles as the inter-probe delay AND a prompt
        # stop: it returns True the instant `close()` sets the event, so an
        # idle worker joins without waiting out the interval.
        while not self._stop.wait(self._interval_s):
            if not self._active.is_set():
                continue  # stood down (paused): skip this probe
            try:
                result = self._probe()
            except Exception as exc:  # a background probe must never kill the worker
                log.warning("bg_detect.probe_failed", error=str(exc))
                continue
            if result.healthy:
                continue
            with self._lock:
                if not self._active.is_set():
                    continue  # stood down while this probe was in flight: drop it
                self._pending = result
            log.info(
                "bg_detect.alert", state=result.state.value, reason=result.reason
            )

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

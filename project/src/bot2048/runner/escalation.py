"""Escalation machinery (docs/ops-monitoring.md §4-§5).

* :func:`probe_screen` -- one full observation: fresh screencap (the device
  layer already retries 0-byte captures), fall back to ``ui_dump`` when the
  screen is FLAG_SECURE-blind, classify via ``detect_state``.
* :func:`confirm_incident` -- the mandatory "verify with a fresh screenshot"
  rule before raising anything above WARN.
* :data:`POLICIES` -- per-code WARN-retry budgets and hard-FATAL flags, as
  data (§5 table).
* :class:`RepeatCounter` -- "same code >= 3 times in 10 minutes" escalator.

The supervisor (bot2048.device.supervisor) consumes these to run its single
ERROR recovery cycle; AUTH_REQUIRED and CRASH bypass recovery entirely
(``fatal_immediately``) -- banking app, no taps, wait for the operator.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any, Protocol

import structlog

from bot2048.device import ui_map
from bot2048.device.adb import DeviceError, EmptyScreencapError
from bot2048.runner.alerts import AlertCode
from bot2048.runner.incidents import Image
from bot2048.runner.live_config import LiveConfig
from bot2048.vision.states import ScreenState, StateDetection

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_ATTEMPTS",
    "POLICIES",
    "SCREEN_UNEXPECTED_REPOLLS",
    "SCREEN_UNEXPECTED_REPOLL_S",
    "SECURE_UI_DUMP_RETRIES",
    "SECURE_UI_DUMP_RETRY_WAIT_S",
    "Classifier",
    "CodePolicy",
    "ConfirmResult",
    "ProbeDevice",
    "ProbeResult",
    "RepeatCounter",
    "confirm_incident",
    "policy_for",
    "probe_screen",
]

#: Board is "valid" when at least this fraction of cells match the palette (§4).
MIN_BOARD_MATCH_FRAC: float = 0.9

#: ``ui_dump`` retries after a 0-byte (FLAG_SECURE) screencap. ``uiautomator
#: dump`` frequently returns no hierarchy during transitions/animations
#: (docs/device-ui-notes.md), so one blank dump proves nothing -- wait and
#: re-dump a couple of times before drawing ANY conclusion.
SECURE_UI_DUMP_RETRIES: int = 2

#: Pause between those ``ui_dump`` retries (seconds).
SECURE_UI_DUMP_RETRY_WAIT_S: float = 0.4


class ProbeDevice(Protocol):
    """Observation-only device surface used by probing (no taps here)."""

    def screenshot(self) -> Image: ...
    def ui_dump(self) -> str: ...


Classifier = Callable[[Image], StateDetection]
"""``detect_state`` bound to calibration+anchors (injectable for tests)."""


@dataclass(frozen=True, slots=True)
class CodePolicy:
    """Per-code retry/escalation policy (§5) -- THE retry table, as data.

    ``warn_retries`` is the number of automatic retries spent on a failure
    before it is recorded as an incident (so ``max_attempts`` = retries + 1
    tries in total); ``backoff_s`` the pauses between them; ``source`` names
    the knob that ENFORCES the budget at runtime (the policy mirrors it --
    see :func:`_live_default`), so the table and the loop can never drift
    apart silently; ``note`` is the one-line human rationale that ends up in
    ``alert.json["retry_policy"]`` / the incident bundle.
    """

    warn_retries: int
    backoff_s: tuple[float, ...]
    fatal_immediately: bool = False  # skip WARN and ERROR-recovery entirely
    source: str = "runner.escalation.POLICIES"
    note: str = ""

    @property
    def max_attempts(self) -> int:
        """Total tries incl. the first one (``1`` for fatal-immediately codes)."""
        return self.warn_retries + 1

    def describe(self) -> dict[str, Any]:
        """JSON-ready view for ``alert.json["retry_policy"]`` (schema v2)."""
        return {
            "max_attempts": self.max_attempts,
            "retries": self.warn_retries,
            "backoff_s": list(self.backoff_s),
            "fatal_immediately": self.fatal_immediately,
            "source": self.source,
            "note": self.note,
        }


def _live_default(name: str) -> Any:
    """Default value of a :class:`LiveConfig` field (the enforcing knob).

    The loop enforces most retry budgets through ``LiveConfig`` (CLI-tunable);
    the policy table reads those defaults so there is ONE source of truth and
    the table can never advertise a budget the loop does not apply.
    """
    for f in fields(LiveConfig):
        if f.name == name:
            return f.default
    raise KeyError(name)  # pragma: no cover - programming error


#: Default number of attempts for a code that has no dedicated runtime knob
#: (the §5 convention: "retry ~3 times, then record the incident").
DEFAULT_ATTEMPTS: int = 3

#: Tap-less re-polls of a NOT_IN_GAME / UNKNOWN screen before the supervisor
#: FATALs (the SCREEN_UNEXPECTED budget). The window must OUTLAST the game's
#: animated cat-mascot paw-reach (it covers the top-centre board cells for a
#: few seconds, dropping board-match to ~44% -- LIVE incident 20260711T2040,
#: score 34052 killed at ply 1859): 20 x 0.5 s ~= 10 s. A genuinely stuck
#: non-game screen stays non-GAME the whole window and still FATALs (blind
#: taps forbidden). ``SupervisorConfig.transient_repolls`` reads THIS value.
SCREEN_UNEXPECTED_REPOLLS: int = 20
SCREEN_UNEXPECTED_REPOLL_S: float = 0.5

#: §5 retry table -- the single source every incident reports against.
#:
#: AUTH_REQUIRED / CRASH / RESTART_LOOP go straight to FATAL: bank app -- no
#: taps, no auto-recovery; the operator resolves them. WATCHDOG_TIMEOUT is
#: monitor-side only (never raised by the runner) and listed for completeness.
#: Where a runtime knob enforces the budget the row MIRRORS that knob
#: (``source``); change the knob, and the table follows.
POLICIES: dict[AlertCode, CodePolicy] = {
    AlertCode.DEVICE_LOST: CodePolicy(
        DEFAULT_ATTEMPTS,
        (0.5, 1.0, 2.0),
        source="device.adb screencap attempts (3) -> supervisor -> SessionRecovery",
        note="3 screencap attempts in the adb layer; then ERROR -> FATAL -> session "
        "recovery (reconnect + backoff) under LiveConfig.recover_max_per_hour",
    ),
    AlertCode.AUTH_REQUIRED: CodePolicy(
        0,
        (),
        fatal_immediately=True,
        source="LiveConfig.auth_retries (auto-login only)",
        note="bank auth wall: FATAL with zero taps; opt-in auto-login types the PIN "
        f"up to LiveConfig.auth_retries={_live_default('auth_retries')} times first",
    ),
    AlertCode.VISION_MISMATCH: CodePolicy(
        int(_live_default("parse_retries")),
        (0.5, 1.0, 2.0),
        source="LiveConfig.parse_retries",
        note="stabilized re-captures per verification (+ occlusion slow retries "
        f"LiveConfig.occlusion_max_retries={_live_default('occlusion_max_retries')} "
        "and the auto-learn gate) before the move is declared a mismatch",
    ),
    AlertCode.MOVE_REJECTED: CodePolicy(
        int(_live_default("swipe_retries")),
        tuple([float(_live_default("swipe_retry_delay_s"))] * int(_live_default("swipe_retries"))),
        source="LiveConfig.swipe_retries",
        note="re-swipes when the board did not change; total attempts = retries + 1",
    ),
    AlertCode.GAME_OVER_UNEXPECTED: CodePolicy(
        1,
        (0.5,),
        source="Supervisor.start_new_game + SessionRecovery",
        note="one restart flow; a failed restart FATALs and the session recovery "
        "retries it under LiveConfig.recover_max_per_hour",
    ),
    AlertCode.SCREEN_UNEXPECTED: CodePolicy(
        SCREEN_UNEXPECTED_REPOLLS,
        (SCREEN_UNEXPECTED_REPOLL_S,) * SCREEN_UNEXPECTED_REPOLLS,
        source="escalation.SCREEN_UNEXPECTED_REPOLLS (SupervisorConfig.transient_repolls)",
        note="tap-less re-polls of a NOT_IN_GAME/UNKNOWN screen (outlasts the mascot "
        "paw animation); CONFIRM_NEW_GAME/SCREEN_OFF get one calibrated recovery",
    ),
    AlertCode.CRASH: CodePolicy(
        0,
        (),
        fatal_immediately=True,
        source="lifecycle.run_session",
        note="uncaught exception: always FATAL, exit(2)",
    ),
    AlertCode.STORAGE_ERROR: CodePolicy(
        0,
        (),
        source="runner (WARN only)",
        note="SQLite write failure is a WARN and the game keeps going; no retry of the "
        "transaction is implemented (the AsyncRecorder re-queues internally)",
    ),
    AlertCode.RESTART_LOOP: CodePolicy(
        0,
        (),
        fatal_immediately=True,
        source="lifecycle.enforce_restart_budget / LiveConfig.low_score_churn_limit",
        note="restart budget exceeded (FATAL, refuse to start) or a churn loop of "
        "very short games (ERROR, session continues)",
    ),
    AlertCode.LOOP_ERROR: CodePolicy(
        int(_live_default("recover_max_per_hour")),
        (float(_live_default("recover_backoff_s")),) * int(_live_default("recover_max_per_hour")),
        source="LiveConfig.recover_max_per_hour",
        note="in-place session recoveries per sliding hour after an unexpected "
        "exception / transient FATAL; over budget -> the cause propagates (CRASH)",
    ),
    AlertCode.WATCHDOG_TIMEOUT: CodePolicy(
        0,
        (),
        fatal_immediately=True,
        source="tgnotify.watch (monitor-side)",
        note="synthesized by the watcher when heartbeat.json is stale; never raised here",
    ),
}


def policy_for(code: AlertCode) -> CodePolicy:
    """The retry policy of ``code`` (a conservative default for unknown codes)."""
    return POLICIES.get(code, CodePolicy(DEFAULT_ATTEMPTS - 1, (), source="default"))


class RepeatCounter:
    """Sliding-window repeat detector: same code >= ``threshold`` in
    ``window_s`` seconds forces escalation even if each incident looked
    recoverable on its own (§4 item 3)."""

    def __init__(
        self,
        window_s: float = 600.0,
        threshold: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_s = window_s
        self.threshold = threshold
        self._clock = clock
        self._hits: dict[AlertCode, deque[float]] = defaultdict(deque)

    def bump(self, code: AlertCode) -> int:
        """Record one occurrence; return the count inside the window."""
        now = self._clock()
        hits = self._hits[code]
        hits.append(now)
        while hits and hits[0] < now - self.window_s:
            hits.popleft()
        return len(hits)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One observation of the screen; ``image`` is None when screencap is blind."""

    state: ScreenState
    image: Image | None
    uidump_xml: str | None
    board_match_frac: float
    reason: str

    @property
    def healthy(self) -> bool:
        return (
            self.state is ScreenState.GAME
            and self.board_match_frac >= MIN_BOARD_MATCH_FRAC
        )


def probe_screen(
    device: ProbeDevice,
    classify: Classifier,
    *,
    frames_flowing: Callable[[], bool] | None = None,
    secure_retries: int = SECURE_UI_DUMP_RETRIES,
    retry_wait_s: float = SECURE_UI_DUMP_RETRY_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> ProbeResult:
    """Fresh screencap -> (ui_dump fallback) -> detect_state.

    Never raises on device trouble -- it degrades into ``DEVICE_LOST`` /
    ``AUTH_REQUIRED`` / ``SECURE_UNCONFIRMED`` / ``UNKNOWN`` results the
    caller escalates on.

    SAFETY (incident 2026-07: false ``device_lost`` on the auth wall): a
    0-byte screencap is the DOCUMENTED FLAG_SECURE signature of the bank's
    auth/PIN surface (docs/device-ui-notes.md) -- and the adb link ANSWERED
    that screencap, so on its own it must never be read as a lost device.
    ``uiautomator dump`` in turn frequently yields no hierarchy during
    transitions, so a single blank dump proves nothing either. Mislabeling
    that pair as ``DEVICE_LOST`` would route the bank's auth surface into the
    retry/recovery path instead of the taps-forbidden AUTH path. Therefore:

    * ``EmptyScreencapError`` retries ``ui_dump`` up to ``secure_retries``
      more times (``retry_wait_s`` apart) before concluding anything;
    * with all dumps blank/failed, ``frames_flowing`` (APK board packets
      recently received) decides: frames flowing -> the device is
      demonstrably alive -> ``SECURE_UNCONFIRMED`` (non-tap hold state),
      NEVER ``DEVICE_LOST``;
    * ``DEVICE_LOST`` is reachable from this branch only when screencap is
      blind AND ``ui_dump`` failed every attempt AND the APK frame stream
      has ALSO stopped (``frames_flowing`` returned False);
    * without an APK source (``frames_flowing=None``) the blind pair still
      resolves to ``SECURE_UNCONFIRMED`` -- a real disconnect surfaces as a
      plain :class:`DeviceError` from ``screenshot`` (screencap did NOT
      answer) and keeps returning ``DEVICE_LOST`` below.
    """
    try:
        image = device.screenshot()
    except EmptyScreencapError:
        attempts = 1 + max(0, secure_retries)
        last_error = "ui_dump not attempted"
        for attempt in range(attempts):
            if attempt:
                sleep(retry_wait_s)
            try:
                xml = device.ui_dump()
            except DeviceError as exc:
                # Blank hierarchy mid-transition is common -- retry, never
                # conclude from one failed dump (device-ui-notes.md).
                last_error = str(exc)
                continue
            if not xml.strip():
                last_error = "ui_dump returned empty output"
                continue
            if ui_map.AUTH_TEXT_ANCHOR in xml:
                return ProbeResult(
                    ScreenState.AUTH_REQUIRED,
                    None,
                    xml,
                    0.0,
                    "screencap 0 bytes (FLAG_SECURE) + "
                    f"uiautomator anchor {ui_map.AUTH_TEXT_ANCHOR!r} matched",
                )
            return ProbeResult(
                ScreenState.UNKNOWN,
                None,
                xml,
                0.0,
                "screencap 0 bytes (FLAG_SECURE) but no auth anchor in ui dump",
            )
        if frames_flowing is not None and not frames_flowing():
            # Screencap blind + no hierarchy after retries + the APK board
            # stream has stopped too: every liveness surface is dead -- this
            # is the ONLY way a 0-byte screencap may end in DEVICE_LOST.
            return ProbeResult(
                ScreenState.DEVICE_LOST,
                None,
                None,
                0.0,
                f"screencap 0 bytes, ui_dump failed {attempts}x "
                f"({last_error}) and APK frames stopped",
            )
        alive_hint = (
            "APK frames still flowing"
            if frames_flowing is not None
            else "no APK frame source to consult; screencap answered"
        )
        return ProbeResult(
            ScreenState.SECURE_UNCONFIRMED,
            None,
            None,
            0.0,
            f"screencap 0 bytes (FLAG_SECURE signature), ui_dump blank/failed "
            f"{attempts}x ({last_error}), {alive_hint} -> secure surface "
            "unconfirmed, NOT device_lost (no taps)",
        )
    except DeviceError as exc:
        if frames_flowing is not None and frames_flowing():
            # The screencap channel errored but board packets are still
            # arriving: the device is demonstrably alive (adb hiccup, busy
            # channel). Non-fatal UNKNOWN -- the next probe re-observes;
            # never a false DEVICE_LOST while frames flow.
            return ProbeResult(
                ScreenState.UNKNOWN,
                None,
                None,
                0.0,
                f"screenshot failed ({exc}) but APK frames still flowing "
                "-> device alive, NOT device_lost",
            )
        return ProbeResult(
            ScreenState.DEVICE_LOST, None, None, 0.0, f"screenshot failed: {exc}"
        )
    detection = classify(image)
    return ProbeResult(
        detection.state, image, None, detection.board_match_frac, detection.reason
    )


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    """Outcome of :func:`confirm_incident`."""

    escalate: bool  # False -> WARN + auto-retry; True -> ERROR path
    code: AlertCode  # possibly overridden (AUTH_REQUIRED / DEVICE_LOST found)
    probe: ProbeResult
    attempt: int  # occurrence count of `code` inside the repeat window
    reason: str


def confirm_incident(
    device: ProbeDevice,
    classify: Classifier,
    code: AlertCode,
    *,
    repeats: RepeatCounter | None = None,
) -> ConfirmResult:
    """§4 rule: before raising anything above WARN, LOOK at the screen.

    Decision:

    * probe shows healthy GAME (state GAME, board_match_frac >= 0.9) and the
      code is not repeating -> ``escalate=False``: re-sync from the PARSED
      board (never the cache) and auto-retry;
    * anything else -- non-GAME state, invalid board, `fatal_immediately`
      code, or >= 3 same-code hits in 10 min -> ``escalate=True``.

    The returned ``code`` may be upgraded to what the probe actually found
    (AUTH_REQUIRED / DEVICE_LOST), which is more actionable than the
    original symptom.
    """
    probe = probe_screen(device, classify)
    attempt = repeats.bump(code) if repeats is not None else 1

    final_code = code
    if probe.state is ScreenState.AUTH_REQUIRED:
        final_code = AlertCode.AUTH_REQUIRED
    elif probe.state is ScreenState.DEVICE_LOST:
        final_code = AlertCode.DEVICE_LOST

    policy = POLICIES.get(final_code)
    fatal_now = policy.fatal_immediately if policy is not None else False
    repeated = repeats is not None and attempt >= repeats.threshold
    escalate = fatal_now or repeated or not probe.healthy

    if not probe.healthy:
        reason = f"screen not healthy: {probe.reason}"
    elif fatal_now:
        reason = f"{final_code.value} is fatal by policy"
    elif repeated:
        reason = f"{final_code.value} repeated {attempt}x within window"
    else:
        reason = "screen healthy (GAME, board valid) -- WARN, auto-retry"

    log.info(
        "escalation.confirm",
        code=final_code.value,
        escalate=escalate,
        state=probe.state.value,
        board_match_frac=round(probe.board_match_frac, 3),
        attempt=attempt,
    )
    return ConfirmResult(
        escalate=escalate, code=final_code, probe=probe, attempt=attempt, reason=reason
    )

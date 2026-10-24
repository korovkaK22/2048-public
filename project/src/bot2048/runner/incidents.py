"""Incident bundles and THE one incident path (docs/ops-monitoring.md §4).

``logs/incidents/<UTC-ts>_<code>/`` -- everything the operator needs to
diagnose an ERROR/FATAL without touching the phone: screenshots before/after
(the ``after`` one FRESH at incident time), ui-dump (only when screencap is
blind), board expectation vs parse, the last N moves, state-probe details,
traceback and run context.

Every terminal failure of the live loop -- a retry budget exhausted, a FATAL,
a session recovery, a crash -- goes through :meth:`IncidentSink.record`, which
does, in this order:

1. decide the bundle directory (new / chained ERROR->FATAL / de-duplicated);
2. obtain the screenshots: the last healthy frame (``screenshot_before.png``),
   the frame the pipeline last parsed (``screenshot_last_parsed.png``) and a
   frame AT incident time (``screenshot_after.png`` -- the probe frame when the
   caller just took one, otherwise a FRESH screencap through the ring's capture
   hook; a capture failure is RECORDED in the bundle, never raised);
3. write the artifacts (best-effort per file -- a failed artifact write is
   logged and skipped, it must never mask the original problem);
4. write the alert row (schema v2) to ``alerts.jsonl`` -- which fans out to
   Telegram through the writer's notifier hook, photo included;
5. copy the alert into the bundle (``alert.json``) and mirror it into
   ``live_events`` with ``incident_dir`` set;
6. prune the incidents root (oldest first, never the current run).

Bundle-level rules:

* **chaining** -- a FATAL that follows an ERROR of the same code within
  :data:`CHAIN_WINDOW_S` (the "ERROR -> one recovery cycle -> FATAL" ladder)
  lands in the SAME directory as ``alert_fatal.json`` /
  ``screenshot_fatal.png`` / ``state_probes_fatal.json`` / ``traceback_fatal.txt``
  -- one incident, one folder (the old ``-2`` sibling directories are gone);
* **de-duplication** -- a repeat of the same code (any level) within
  :data:`DEDUPE_WINDOW_S` appends one line to ``alerts_repeat.jsonl`` in the
  previous bundle instead of a new directory (no new screenshots): a storm
  costs one bundle per window, every alert row still points at a folder;
* **retention** -- :func:`prune_incidents` keeps the root under
  :data:`DEFAULT_MAX_TOTAL_BYTES` / :data:`DEFAULT_MAX_BUNDLES`, deleting the
  oldest bundles first and never a bundle of the current run.

Privacy: bundles hold screenshots of a BANKING app. They live under ``logs/``
(gitignored) and are only ever sent to the owner's private Telegram chat.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt
import structlog

from bot2048.runner.alerts import Alert, AlertCode, AlertLevel, AlertWriter
from bot2048.storage.errors import StorageError
from bot2048.vision.states import ScreenState

log = structlog.get_logger(__name__)

__all__ = [
    "BUNDLE_DIR_RE",
    "CHAIN_WINDOW_S",
    "DEDUPE_WINDOW_S",
    "DEFAULT_INCIDENTS_ROOT",
    "DEFAULT_MAX_BUNDLES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "LAST_MOVES_LIMIT",
    "CaptureHook",
    "IncidentRecord",
    "IncidentSink",
    "MoveRing",
    "OpsSink",
    "ProbeLike",
    "prune_incidents",
    "write_png",
]

DEFAULT_INCIDENTS_ROOT: Path = Path("logs") / "incidents"

LAST_MOVES_LIMIT: int = 20

#: Disk budget of the incidents root (bytes). Generous: a bundle is ~1-1.5 MB
#: (two full-screen PNGs), so 2 GiB holds well over a thousand incidents.
DEFAULT_MAX_TOTAL_BYTES: int = 2 * 1024**3

#: Count budget of the incidents root (bundle directories).
DEFAULT_MAX_BUNDLES: int = 1000

#: A FATAL this soon after an ERROR of the same code is the SAME incident
#: (the supervisor's one recovery cycle sits in between: up to 20 re-polls x
#: 0.5 s + wake/settle pauses).
CHAIN_WINDOW_S: float = 120.0

#: The same code again (any level) within this window -> no new bundle, one
#: line in the previous bundle's ``alerts_repeat.jsonl``. The window counts
#: from the bundle's FIRST alert, so a continuous storm still gets a fresh
#: bundle (new screenshot) once per window.
DEDUPE_WINDOW_S: float = 30.0

#: Bundle directory names: ``20260710T140322Z_AUTH_REQUIRED`` (+ ``-N``).
BUNDLE_DIR_RE: re.Pattern[str] = re.compile(r"^(\d{8}T\d{6}Z)_[A-Z_]+(?:-\d+)?$")

Image = npt.NDArray[np.uint8]

CaptureHook = Callable[[], "Image | None"]
"""Zero-arg FRESH full-frame screencap (RGB uint8) -- may raise."""


class ProbeLike(Protocol):
    """The shape of :class:`bot2048.runner.escalation.ProbeResult` this module
    needs (declared here to keep ``incidents`` import-free of ``escalation``,
    which imports this module)."""

    @property
    def state(self) -> ScreenState: ...
    @property
    def image(self) -> Image | None: ...
    @property
    def uidump_xml(self) -> str | None: ...
    @property
    def board_match_frac(self) -> float: ...
    @property
    def reason(self) -> str: ...


class OpsSink(Protocol):
    """``OpsRepository.record_event`` -- the only ops call the sink makes."""

    def record_event(
        self,
        run_id: str,
        level: str,
        code: str,
        message: str,
        *,
        game_id: str | None = ...,
        ply: int | None = ...,
        details: dict[str, Any] | None = ...,
        incident_dir: str | None = ...,
        ts: float | None = ...,
    ) -> None: ...


class MoveRing:
    """Ring buffer of the last N moves + the last known-good frame + the
    fresh-capture hook.

    The runner feeds it on every move; incident bundles snapshot it. It is
    the ONE object shared by the lifecycle guard, the supervisor and the live
    runner, so it also carries the :data:`CaptureHook` the runner installs
    (``device.screenshot`` under the shared capture lock): every incident
    sink -- including the lifecycle CRASH handler, which has no device of its
    own -- can take a FRESH screenshot at incident time through it. All
    methods are thread-safe (the heartbeat/watchdog thread may race the game
    loop).
    """

    def __init__(self, maxlen: int = LAST_MOVES_LIMIT) -> None:
        self._moves: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._good_frame: Image | None = None
        self._capture: CaptureHook | None = None
        self._lock = threading.Lock()

    def record_move(
        self,
        *,
        ply: int,
        direction: str,
        board_before: str,
        board_after: str,
        eval_ms: float | None = None,
        score_total: int | None = None,
    ) -> None:
        with self._lock:
            self._moves.append(
                {
                    "ply": ply,
                    "direction": direction,
                    "board_before": board_before,
                    "board_after": board_after,
                    "eval_ms": eval_ms,
                    "score_total": score_total,
                }
            )

    def set_good_frame(self, image: Image) -> None:
        """Remember the latest frame that classified as healthy GAME."""
        with self._lock:
            self._good_frame = image

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._moves)

    @property
    def good_frame(self) -> Image | None:
        with self._lock:
            return self._good_frame

    # -- fresh capture at incident time ---------------------------------------

    def set_capture(self, hook: CaptureHook | None) -> None:
        """Install (or clear) the fresh-screencap hook incidents use."""
        with self._lock:
            self._capture = hook

    @property
    def has_capture(self) -> bool:
        with self._lock:
            return self._capture is not None

    def fresh_frame(self) -> tuple[Image | None, str | None]:
        """One fresh screencap through the hook: ``(frame, error)``.

        Never raises: a missing hook, a raising hook (device gone, 0-byte
        FLAG_SECURE screencap) or an empty frame come back as ``(None,
        reason)`` so the caller can RECORD why there is no screenshot.
        """
        with self._lock:
            hook = self._capture
        if hook is None:
            return None, "no fresh-capture hook installed"
        try:
            frame = hook()
        except Exception as exc:  # device trouble is the incident itself
            return None, f"{type(exc).__name__}: {exc}"
        if frame is None or getattr(frame, "size", 0) == 0:
            return None, "capture returned an empty frame"
        return frame, None


# -- artifact writers ---------------------------------------------------------


def _write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        log.warning("incident.artifact_failed", file=path.name, error=str(exc))


def _write_json(path: Path, data: Any) -> None:
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as exc:
        log.warning("incident.artifact_failed", file=path.name, error=str(exc))


def _append_jsonl(path: Path, data: Any) -> None:
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        log.warning("incident.artifact_failed", file=path.name, error=str(exc))


def write_png(path: Path, image: Image) -> None:
    """Best-effort PNG write of an RGB uint8 frame (the shared artifact writer).

    Public on purpose: the live loop's end-of-game screen
    (``LiveRunner._save_game_screen``) and the on-error debug bundles
    (:mod:`bot2048.runner.debug_capture`) reuse it, so every runner-side
    screenshot artifact shares ONE contract -- a Pillow/array failure is
    logged at WARN and swallowed, never raised (the write must not mask the
    problem being documented).
    """
    try:
        pil_image = importlib.import_module("PIL.Image")
        pil_image.fromarray(np.asarray(image, dtype=np.uint8)).save(path)
    except Exception as exc:  # Pillow missing / bad array -- never fatal here
        log.warning("incident.artifact_failed", file=path.name, error=str(exc))


# -- retention ----------------------------------------------------------------


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def prune_incidents(
    root: Path,
    *,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_bundles: int = DEFAULT_MAX_BUNDLES,
    protect: Callable[[Path], bool] | None = None,
) -> tuple[int, int]:
    """Delete the OLDEST bundles until ``root`` fits both budgets.

    Bundle directories sort by their UTC-timestamp prefix (= creation order).
    ``protect(dir)`` returning True exempts a bundle (the current run's
    bundles are never pruned). Returns ``(bundles_removed, bytes_removed)``;
    never raises -- retention is housekeeping, not the incident.
    """
    try:
        bundles = sorted(
            p for p in root.iterdir() if p.is_dir() and BUNDLE_DIR_RE.match(p.name)
        )
    except OSError:
        return 0, 0
    sizes = {p: _dir_size(p) for p in bundles}
    total = sum(sizes.values())
    count = len(bundles)
    removed = 0
    freed = 0
    for bundle in bundles:  # oldest first
        if count <= max_bundles and total <= max_total_bytes:
            break
        if protect is not None and protect(bundle):
            continue
        try:
            shutil.rmtree(bundle)
        except OSError as exc:
            log.warning("incident.prune_failed", dir=bundle.name, error=str(exc))
            continue
        removed += 1
        freed += sizes[bundle]
        count -= 1
        total -= sizes[bundle]
    if removed:
        log.info(
            "incident.pruned",
            removed=removed,
            freed_bytes=freed,
            remaining=count,
            remaining_bytes=total,
        )
    return removed, freed


# -- the sink -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    """What :meth:`IncidentSink.record` produced."""

    alert: Alert
    bundle_dir: Path
    chained: bool = False  # FATAL written into the preceding ERROR's bundle
    attached: bool = False  # repeat / attach: no new bundle, one jsonl line


@dataclass(slots=True)
class _Last:
    code: AlertCode
    level: AlertLevel
    bundle_dir: Path
    ts: float


class IncidentSink:
    """The one place every terminal failure is recorded (module docs).

    Args:
        root: incidents root (``logs/incidents``).
        alerts: the alerts.jsonl writer (its notifier hook is the Telegram path).
        ops: ``live_events`` writer (``OpsRepository``); ``None`` = no DB mirror.
        run_id: current run (alert + context + ``live_events.run_id``).
        ring: the shared :class:`MoveRing` -- last moves, last good frame and
            the fresh-capture hook.
        context_provider: zero-arg callable returning session context that
            goes into ``context.json["session"]``; its ``strategy`` /
            ``spec_hash`` / ``engine`` keys also fill the v2 alert fields.
            Installed by the live runner (which knows the strategy).
        last_frame_provider: zero-arg callable returning the frame behind the
            latest parse (``screenshot_last_parsed.png``).
        max_total_bytes / max_bundles: retention budgets (oldest-first).
        chain_window_s / dedupe_window_s: see module docs.
        clock: wall clock (``time.time``), injectable for tests.
    """

    def __init__(
        self,
        *,
        root: Path = DEFAULT_INCIDENTS_ROOT,
        alerts: AlertWriter,
        ops: OpsSink | None = None,
        run_id: str | None = None,
        ring: MoveRing | None = None,
        context_provider: Callable[[], dict[str, Any]] | None = None,
        last_frame_provider: Callable[[], Image | None] | None = None,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_bundles: int = DEFAULT_MAX_BUNDLES,
        chain_window_s: float = CHAIN_WINDOW_S,
        dedupe_window_s: float = DEDUPE_WINDOW_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.alerts = alerts
        self.ops = ops
        self.run_id = run_id
        self.ring = ring
        self.context_provider = context_provider
        self.last_frame_provider = last_frame_provider
        self.max_total_bytes = max_total_bytes
        self.max_bundles = max_bundles
        self.chain_window_s = chain_window_s
        self.dedupe_window_s = dedupe_window_s
        self._clock = clock
        #: Bundles created at/after this instant belong to the current run and
        #: are never pruned (a run may legitimately produce many incidents).
        self.started_at: float = clock()
        self._created: set[str] = set()
        self._last: _Last | None = None
        self._lock = threading.RLock()

    # -- public API -----------------------------------------------------------

    @property
    def last_dir(self) -> str | None:
        """Relative POSIX path of the most recent bundle (for ``MOVE_ABORTED`` rows)."""
        with self._lock:
            return self._last.bundle_dir.as_posix() if self._last is not None else None

    def record(
        self,
        code: AlertCode,
        level: AlertLevel,
        message: str,
        *,
        probe: ProbeLike | None = None,
        game_id: str | None = None,
        move_no: int | None = None,
        board_hex: str | None = None,
        attempt: int | None = None,
        attempts: int | None = None,
        retry_policy: dict[str, Any] | None = None,
        stage: str | None = None,
        extra: dict[str, Any] | None = None,
        traceback_text: str | None = None,
        board: dict[str, Any] | None = None,
        attach_to: str | None = None,
    ) -> IncidentRecord:
        """Record one terminal failure: bundle + alert + DB row (module docs).

        ``probe`` is the observation the caller already has (its ``image`` is
        the incident-time frame; ``None`` image -> a fresh screencap is taken
        through the ring unless the probe says the screen is blind/lost).
        ``attach_to`` names an EXISTING bundle to append this alert to
        (e.g. the session-recovery alert after a FATAL whose bundle is already
        on disk). Never raises for artifact/DB trouble; only ``alerts.write``
        itself (the source of truth) may propagate an ``OSError``.
        """
        now = self._clock()
        extra = dict(extra or {})
        with self._lock:
            target, chained, attached = self._choose_dir(code, level, now, attach_to)
            if not attached and not target.exists():
                try:
                    target.mkdir(parents=True)
                    self._created.add(target.name)
                except OSError as exc:
                    # Losing the bundle is bad but the alert must still go out.
                    log.error("incident.bundle_dir_failed", dir=str(target), error=str(exc))
            session = self._session_context()
            capture_kind: str | None = None
            capture_error: str | None = None
            if not attached:
                capture_kind, capture_error = self._write_artifacts(
                    target,
                    code=code,
                    level=level,
                    message=message,
                    probe=probe,
                    chained=chained,
                    game_id=game_id,
                    move_no=move_no,
                    attempt=attempt,
                    attempts=attempts,
                    retry_policy=retry_policy,
                    stage=stage,
                    extra=extra,
                    traceback_text=traceback_text,
                    board=board,
                    session=session,
                    now=now,
                )
                if capture_error is not None:
                    extra.setdefault("capture_error", capture_error)
                if chained:
                    extra.setdefault("chained_from", "alert.json")
            else:
                extra.setdefault("repeat_of", target.as_posix())
            alert = Alert(
                level=level,
                code=code,
                message=message,
                run_id=self.run_id,
                game_id=game_id,
                move_no=move_no,
                screen_state=probe.state.value if probe is not None else None,
                board_hex=board_hex,
                attempt=attempt,
                incident_dir=target.as_posix(),
                extra=extra,
                attempts=attempts,
                retry_policy=retry_policy,
                capture_kind=capture_kind,
                stage=stage,
                strategy=_opt_str(session.get("strategy")),
                spec_hash=_opt_str(session.get("spec_hash")),
                engine=_opt_dict(session.get("engine")),
            )
            # The alert row is the source of truth (and the Telegram trigger via
            # the writer's notifier hook): written AFTER the bundle exists so
            # the photo it references is already on disk.
            self.alerts.write(alert)
            if attached:
                _append_jsonl(target / "alerts_repeat.jsonl", alert.to_dict())
            else:
                name = "alert_fatal.json" if chained else "alert.json"
                _write_json(target / name, alert.to_dict())
            self._ops_event(alert)
            if not attached:
                # A repeat keeps the FIRST occurrence's stamp, so a continuous
                # storm still opens a fresh bundle (new screenshot) once per
                # dedupe window instead of attaching forever. A chained FATAL
                # keeps the ERROR's stamp for the same reason.
                stamp = self._last.ts if (chained and self._last is not None) else now
                self._last = _Last(code, level, target, stamp)
                if not chained:
                    self._prune()
            log.info(
                "incident.recorded",
                code=code.value,
                level=level.value,
                dir=target.as_posix(),
                chained=chained,
                attached=attached,
                capture_kind=capture_kind,
                stage=stage,
            )
            return IncidentRecord(alert, target, chained=chained, attached=attached)

    # -- internals --------------------------------------------------------------

    def _choose_dir(
        self, code: AlertCode, level: AlertLevel, now: float, attach_to: str | None
    ) -> tuple[Path, bool, bool]:
        """``(dir, chained, attached)`` per the chaining/dedupe rules."""
        if attach_to is not None:
            existing = Path(attach_to)
            if existing.is_dir():
                return existing, False, True
        last = self._last
        if last is not None and last.code is code and last.bundle_dir.is_dir():
            age = now - last.ts
            # The ERROR -> failed recovery -> FATAL ladder: one incident.
            if (
                last.level is AlertLevel.ERROR
                and level is AlertLevel.FATAL
                and age <= self.chain_window_s
                and not (last.bundle_dir / "alert_fatal.json").exists()
            ):
                return last.bundle_dir, True, False
            # A storm of the same code (any level: the DEVICE_LOST cycle is
            # ERROR, FATAL, LOOP_ERROR-attached, ERROR, FATAL, ... every few
            # seconds) costs ONE bundle per dedupe window.
            if age <= self.dedupe_window_s:
                return last.bundle_dir, False, True
        return self._new_dir(code, now), False, False

    def _new_dir(self, code: AlertCode, now: float) -> Path:
        ts = datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")
        bundle = self.root / f"{ts}_{code.value}"
        suffix = 2
        while bundle.exists():  # same second, same code -- uniquify
            bundle = self.root / f"{ts}_{code.value}-{suffix}"
            suffix += 1
        return bundle

    def _after_frame(self, probe: ProbeLike | None) -> tuple[Image | None, str, str | None]:
        """``(frame, capture_kind, capture_error)`` for ``screenshot_after``."""
        if probe is not None and probe.image is not None:
            return probe.image, "probe", None
        if probe is not None:
            if probe.state is ScreenState.DEVICE_LOST:
                return None, "device_lost", "fresh capture skipped: probe reported device_lost"
            if probe.uidump_xml is not None or probe.state in (
                ScreenState.AUTH_REQUIRED,
                ScreenState.SECURE_UNCONFIRMED,
            ):
                return (
                    None,
                    "blind_secure",
                    "screencap 0 bytes (FLAG_SECURE surface); ui dump saved instead",
                )
        if self.ring is None or not self.ring.has_capture:
            return None, "none", "no fresh-capture hook installed"
        frame, error = self.ring.fresh_frame()
        if frame is None:
            return None, "failed", error
        return frame, "fresh", None

    def _write_artifacts(
        self,
        bundle: Path,
        *,
        code: AlertCode,
        level: AlertLevel,
        message: str,
        probe: ProbeLike | None,
        chained: bool,
        game_id: str | None,
        move_no: int | None,
        attempt: int | None,
        attempts: int | None,
        retry_policy: dict[str, Any] | None,
        stage: str | None,
        extra: dict[str, Any],
        traceback_text: str | None,
        board: dict[str, Any] | None,
        session: dict[str, Any],
        now: float,
    ) -> tuple[str, str | None]:
        sfx = "_fatal" if chained else ""
        after, capture_kind, capture_error = self._after_frame(probe)
        if not chained:
            before = self.ring.good_frame if self.ring is not None else None
            if before is not None:
                write_png(bundle / "screenshot_before.png", before)
            last_parsed = self._last_parsed()
            if last_parsed is not None:
                write_png(bundle / "screenshot_last_parsed.png", last_parsed)
            _write_json(
                bundle / "last_moves.json",
                self.ring.snapshot() if self.ring is not None else [],
            )
        if after is not None:
            after_name = "screenshot_fatal.png" if chained else "screenshot_after.png"
            write_png(bundle / after_name, after)
        if probe is not None and probe.uidump_xml is not None:
            _write_text(bundle / f"uidump{sfx}.xml", probe.uidump_xml)
        if board is not None:
            _write_json(bundle / f"board{sfx}.json", board)
        if probe is not None:
            _write_json(
                bundle / f"state_probes{sfx}.json",
                {
                    "state": probe.state.value,
                    "reason": probe.reason,
                    "board_match_frac": probe.board_match_frac,
                    "image_present": probe.image is not None,
                    "uidump_present": probe.uidump_xml is not None,
                },
            )
        _write_text(
            bundle / f"traceback{sfx}.txt",
            traceback_text if traceback_text is not None else "".join(traceback.format_stack()),
        )
        _write_json(
            bundle / f"context{sfx}.json",
            {
                "schema": 2,
                "created_at": datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "run_id": self.run_id,
                "code": code.value,
                "level": level.value,
                "message": message,
                "game_id": game_id,
                "move_no": move_no,
                "stage": stage,
                "attempt": attempt,
                "attempts": attempts,
                "retry_policy": retry_policy,
                "capture_kind": capture_kind,
                "capture_error": capture_error,
                "chained_from": "alert.json" if chained else None,
                "extra": extra,
                "session": session,
            },
        )
        return capture_kind, capture_error

    def _last_parsed(self) -> Image | None:
        if self.last_frame_provider is None:
            return None
        try:
            return self.last_frame_provider()
        except Exception as exc:  # a diagnostic accessor must never mask the incident
            log.warning("incident.last_frame_failed", error=str(exc))
            return None

    def _session_context(self) -> dict[str, Any]:
        if self.context_provider is None:
            return {}
        try:
            return dict(self.context_provider())
        except Exception as exc:  # same doctrine: context is best-effort
            log.warning("incident.context_failed", error=str(exc))
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _ops_event(self, alert: Alert) -> None:
        """Mirror the alert into ``live_events`` (incident_dir ALWAYS set)."""
        if self.ops is None or self.run_id is None:
            return
        details = dict(alert.extra)
        details["stage"] = alert.stage
        details["attempts"] = alert.attempts
        details["capture_kind"] = alert.capture_kind
        try:
            self.ops.record_event(
                self.run_id,
                alert.level.value,
                alert.code.value,
                alert.message,
                game_id=alert.game_id,
                ply=alert.move_no,
                details=details,
                incident_dir=alert.incident_dir,
            )
        except StorageError as exc:
            # DB trouble is a WARN, never a stop (§2.2) -- plain alert, no bundle.
            self.alerts.write(
                Alert(
                    level=AlertLevel.WARN,
                    code=AlertCode.STORAGE_ERROR,
                    message=f"live_events write failed: {exc}",
                    run_id=self.run_id,
                )
            )

    def _protected(self, bundle: Path) -> bool:
        if bundle.name in self._created:
            return True
        match = BUNDLE_DIR_RE.match(bundle.name)
        if match is None:
            return False
        try:
            created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            return False
        # Second-granular stamps: anything from the run's starting second on is ours.
        return created.timestamp() >= float(int(self.started_at))

    def _prune(self) -> None:
        try:
            prune_incidents(
                self.root,
                max_total_bytes=self.max_total_bytes,
                max_bundles=self.max_bundles,
                protect=self._protected,
            )
        except Exception as exc:  # housekeeping must never mask the incident
            log.warning("incident.prune_failed", error=str(exc))


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _opt_dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None

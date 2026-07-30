"""Incident bundles (docs/ops-monitoring.md §4).

``logs/incidents/<UTC-ts>_<code>/`` -- everything the operator needs to
diagnose an ERROR/FATAL without touching the phone: screenshots before/after,
ui-dump (only when screencap is blind), board expectation vs parse, the last
N moves, state-probe details, traceback and run context.

Rules:

* the bundle is created BEFORE the alert row is written, so ``incident_dir``
  in the alert always exists;
* WARN never creates a bundle (only if it escalates);
* every artifact write is best-effort: a failure to save one file is logged
  and skipped -- it must never mask the original problem;
* retention: the bot deletes nothing; the operator prunes bundles older
  than 14 days during its visits.
"""

from __future__ import annotations

import importlib
import json
import threading
import traceback
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import structlog

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_INCIDENTS_ROOT",
    "LAST_MOVES_LIMIT",
    "MoveRing",
    "create_bundle",
    "write_alert_copy",
]

DEFAULT_INCIDENTS_ROOT: Path = Path("logs") / "incidents"

LAST_MOVES_LIMIT: int = 20

Image = npt.NDArray[np.uint8]


class MoveRing:
    """Ring buffer of the last N moves + the last known-good frame.

    The runner feeds it on every move; incident bundles snapshot it. All
    methods are thread-safe (the heartbeat/watchdog thread may race the
    game loop).
    """

    def __init__(self, maxlen: int = LAST_MOVES_LIMIT) -> None:
        self._moves: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._good_frame: Image | None = None
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


# -- bundle writing -----------------------------------------------------------


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


def _write_png(path: Path, image: Image) -> None:
    try:
        pil_image = importlib.import_module("PIL.Image")
        pil_image.fromarray(np.asarray(image, dtype=np.uint8)).save(path)
    except Exception as exc:  # Pillow missing / bad array -- never fatal here
        log.warning("incident.artifact_failed", file=path.name, error=str(exc))


def create_bundle(
    code: str,
    *,
    root: Path = DEFAULT_INCIDENTS_ROOT,
    screenshot_before: Image | None = None,
    screenshot_after: Image | None = None,
    uidump_xml: str | None = None,
    board: dict[str, Any] | None = None,
    last_moves: list[dict[str, Any]] | None = None,
    state_probe: dict[str, Any] | None = None,
    traceback_text: str | None = None,
    context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Path:
    """Create ``<root>/<UTC-ts>_<code>/`` and fill it (best-effort per file).

    ``traceback_text``: pass ``traceback.format_exc()`` for CRASH; when
    ``None`` a stack snapshot of the current location is stored instead
    (per §4).
    """
    ts = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    bundle = root / f"{ts}_{code}"
    suffix = 2
    while bundle.exists():  # same second, same code -- uniquify
        bundle = root / f"{ts}_{code}-{suffix}"
        suffix += 1
    try:
        bundle.mkdir(parents=True)
    except OSError as exc:
        # Losing the bundle is bad but the alert must still go out.
        log.error("incident.bundle_dir_failed", dir=str(bundle), error=str(exc))
        return bundle

    if screenshot_before is not None:
        _write_png(bundle / "screenshot_before.png", screenshot_before)
    if screenshot_after is not None:
        _write_png(bundle / "screenshot_after.png", screenshot_after)
    if uidump_xml is not None:  # only when screencap was blind (AUTH case)
        _write_text(bundle / "uidump.xml", uidump_xml)
    if board is not None:
        _write_json(bundle / "board.json", board)
    _write_json(bundle / "last_moves.json", list(last_moves or []))
    if state_probe is not None:
        _write_json(bundle / "state_probes.json", state_probe)
    _write_text(
        bundle / "traceback.txt",
        traceback_text if traceback_text is not None else "".join(traceback.format_stack()),
    )
    _write_json(bundle / "context.json", context or {})
    log.info("incident.bundle_created", dir=str(bundle), code=code)
    return bundle


def write_alert_copy(bundle_dir: Path, alert: Any) -> None:
    """Store a pretty copy of the alert row as ``alert.json`` (§4).

    Called AFTER the alert is written to alerts.jsonl (the bundle itself is
    created first so the alert can reference it).
    """
    _write_json(bundle_dir / "alert.json", alert.to_dict())

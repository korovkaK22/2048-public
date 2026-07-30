"""On-error debug artifacts: screenshot + APK packet + board sidecar.

When a move dies on vision the game aborts (``end_reason=vision_mismatch``)
and until now the ONLY trace was a log line -- we could never SEE what the
board actually looked like at that moment (the decorative cat-mascot that
permanently overlaps the top-center cells? a mid-animation frame? a real
vision bug?). :class:`DebugCapture` answers that: on the vision-error hooks
(the occlusion retry/give-up in
:class:`~bot2048.runner.swipe_controller.SwipeController`, every
``vision_mismatch`` abort, and the vision/device game-abort finalization in
:class:`~bot2048.runner.live.LiveRunner`) it saves

* a FRESH full-screen screenshot PNG (what the phone really shows),
* a ``.json`` sidecar with the latest APK board packet
  (:class:`~bot2048.device.apk_capture.BoardSample`), the parsed / predicted
  boards and the per-cell suspect picture handed in by the hook.

Contracts (mirrors the game-screen / incident artifact doctrine):

* **Never disturbs the loop.** :meth:`DebugCapture.capture` is fire-and-forget:
  the screencap + writes run on a daemon worker thread; ANY failure anywhere
  (device gone, Pillow missing, disk full) is a WARN, never an exception on
  the caller's thread. The one synchronous entry point
  (:meth:`write_sidecar`, used at game finalization off the hot path) swallows
  everything the same way.
* **Serialized captures.** The screencap goes through the runner's shared
  capture lock (same adb channel as the gate / bg-detect worker).
* **Bounded disk.** After every capture the directory is pruned to the newest
  :attr:`keep_files` files (PNG + sidecar = 2 files per capture), so an
  overnight campaign cannot fill the disk
  (:attr:`~bot2048.runner.live_config.LiveConfig.debug_capture_keep`).
* **One INFO per capture**: ``live.debug_capture_saved path=... reason=...``.

Disabled (``enabled=False`` /
:attr:`~bot2048.runner.live_config.LiveConfig.debug_capture_on_error` False)
every hook is a cheap no-op.
"""

from __future__ import annotations

import contextlib
import itertools
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from bot2048.runner import incidents
from bot2048.runner.live_config import DEFAULT_DEBUG_CAPTURES_ROOT
from bot2048.storage.schema import board_to_hex

if TYPE_CHECKING:
    from bot2048.runner.incidents import Image

log = structlog.get_logger(__name__)

__all__ = ["DEBUG_CAPTURE_REASONS", "DebugCapture"]

#: Game-abort ``end_reason`` values that warrant a debug bundle at
#: finalization: the vision/device incidents where "what was on screen?" is
#: the whole question. Deliberately EXCLUDES the normal ends (``game_over``,
#: ``win_2048``, ``held_before_win``, ``interrupted``, ``aborted``) -- those
#: already have the plain game-screen PNG and need no sidecar.
DEBUG_CAPTURE_REASONS: frozenset[str] = frozenset(
    {
        "vision_mismatch",
        "screen_unexpected",
        "game_over_unexpected",
        "device_lost",
        "error",
    }
)


def _apk_sample_json(sample: Any) -> dict[str, Any] | None:
    """Serialize a :class:`BoardSample`-shaped object (duck-typed, tolerant)."""
    if sample is None:
        return None
    board = getattr(sample, "board", None)
    board_hex: str | None = None
    if board is not None:
        try:
            board_hex = board_to_hex(tuple(board))
        except Exception:  # malformed test double -- keep the raw list instead
            board_hex = None
    return {
        "board": list(board) if board is not None else None,
        "board_hex": board_hex,
        "seq": getattr(sample, "seq", None),
        "changed": getattr(sample, "changed", None),
        "not_in_game": getattr(sample, "not_in_game", None),
        "secure_suspected": getattr(sample, "secure_suspected", None),
        "uncertain": getattr(sample, "uncertain", None),
        "age_ms": getattr(sample, "age_ms", None),
    }


def _safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in text)


class DebugCapture:
    """Best-effort on-error capture sink (see module docs).

    Args:
        screenshot: zero-arg fresh full-frame capture (RGB uint8 array or
            ``None``); typically ``lambda: device.screenshot()``. May raise --
            failures land in the sidecar as ``screenshot_error``.
        apk_latest: zero-arg accessor for the newest on-device board packet
            (:meth:`~bot2048.device.apk_capture.ApkBoardSource.latest`);
            ``None`` when no APK source is wired. Snapshotted on the CALLER's
            thread (an in-memory mailbox read, instant) so the packet reflects
            the moment of the event, not the moment the worker ran.
        root: capture directory (default
            :data:`~bot2048.runner.live_config.DEFAULT_DEBUG_CAPTURES_ROOT`).
        keep_files: prune-oldest bound on files in ``root``.
        capture_lock: the runner's shared screencap lock (adb channel).
        enabled: master switch
            (:attr:`~bot2048.runner.live_config.LiveConfig.debug_capture_on_error`);
            ``False`` turns every method into a no-op.
        clock: wall-clock (``time.time``), injectable for tests.
    """

    def __init__(
        self,
        *,
        screenshot: Callable[[], Image | None],
        apk_latest: Callable[[], Any] | None = None,
        root: Path = DEFAULT_DEBUG_CAPTURES_ROOT,
        keep_files: int = 200,
        capture_lock: threading.Lock | None = None,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._screenshot = screenshot
        self._apk_latest = apk_latest
        self.root = Path(root)
        self.keep_files = keep_files
        self._capture_lock = capture_lock
        self.enabled = enabled
        self._clock = clock
        self._seq = itertools.count(1)
        # Serializes file writes + pruning across concurrent capture workers.
        self._io_lock = threading.Lock()

    # -- fire-and-forget capture (hot-path hooks) ---------------------------------

    def capture(
        self,
        *,
        reason: str,
        game_id: str,
        ply: int,
        context: dict[str, Any] | None = None,
    ) -> threading.Thread | None:
        """Save screenshot + sidecar on a background thread; NEVER raises.

        The APK packet + timestamp are snapshotted here (cheap, caller's
        thread, at the true moment of the event); the slow screencap and all
        file I/O run on a daemon worker. Returns the started worker (tests
        ``join()`` it) or ``None`` when disabled / the thread could not start.
        """
        if not self.enabled:
            return None
        try:
            ts = self._clock()
        except Exception:  # a broken injected clock must not kill the move
            ts = time.time()
        sample = self._safe_sample()
        worker = threading.Thread(
            target=self._capture_safe,
            args=(reason, game_id, ply, dict(context or {}), ts, sample),
            name=f"debug-capture-{_safe_name(reason)}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:  # interpreter shutdown / thread limit
            log.warning("live.debug_capture_failed", reason=reason, error=str(exc))
            return None
        return worker

    def _capture_safe(
        self,
        reason: str,
        game_id: str,
        ply: int,
        context: dict[str, Any],
        ts: float,
        sample: Any,
    ) -> None:
        try:
            self._capture_sync(reason, game_id, ply, context, ts, sample)
        except Exception as exc:  # best-effort artifact -- never propagate
            log.warning("live.debug_capture_failed", reason=reason, error=str(exc))

    def _capture_sync(
        self,
        reason: str,
        game_id: str,
        ply: int,
        context: dict[str, Any],
        ts: float,
        sample: Any,
    ) -> None:
        frame: Image | None = None
        screenshot_error: str | None = None
        try:
            if self._capture_lock is not None:
                with self._capture_lock:
                    frame = self._screenshot()
            else:
                frame = self._screenshot()
        except Exception as exc:  # device gone / FLAG_SECURE 0-byte / adb drop
            screenshot_error = f"{type(exc).__name__}: {exc}"
        with self._io_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
            stem = (
                f"{stamp}_{next(self._seq):03d}_{_safe_name(game_id[:8])}"
                f"_ply{ply:04d}_{_safe_name(reason)}"
            )
            png_path = self.root / f"{stem}.png"
            png_name: str | None = None
            if frame is not None and getattr(frame, "size", 1) != 0:
                incidents._write_png(png_path, frame)
                if png_path.exists():  # _write_png swallows Pillow trouble
                    png_name = png_path.name
            json_path = self.root / f"{stem}.json"
            incidents._write_json(
                json_path,
                self._sidecar(
                    reason=reason,
                    game_id=game_id,
                    ply=ply,
                    ts=ts,
                    screenshot=png_name,
                    screenshot_error=screenshot_error,
                    sample=sample,
                    context=context,
                ),
            )
            self._prune_locked()
        log.info(
            "live.debug_capture_saved",
            path=str(png_path if png_name is not None else json_path),
            reason=reason,
            game_id=game_id,
            ply=ply,
        )

    # -- synchronous sidecar (game-finalization path) ------------------------------

    def write_sidecar(
        self,
        artifact_path: Path,
        *,
        reason: str,
        game_id: str,
        ply: int,
        screenshot: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Path | None:
        """Write ``<artifact>.json`` beside an EXISTING game-screen PNG.

        The finalization path already saves ``<game_id>_<end_reason>.png``
        (:meth:`LiveRunner._save_game_screen`); this adds the APK packet +
        parsed-board sidecar to that same path instead of double-saving the
        screenshot. Synchronous (finalization is off the hot path), swallows
        everything, returns the sidecar path or ``None``.
        """
        if not self.enabled:
            return None
        try:
            json_path = artifact_path.with_suffix(".json")
            incidents._write_json(
                json_path,
                self._sidecar(
                    reason=reason,
                    game_id=game_id,
                    ply=ply,
                    ts=self._clock(),
                    screenshot=screenshot,
                    screenshot_error=None,
                    sample=self._safe_sample(),
                    context=dict(context or {}),
                ),
            )
            log.info(
                "live.debug_capture_saved",
                path=str(artifact_path if screenshot is not None else json_path),
                reason=reason,
                game_id=game_id,
                ply=ply,
            )
            return json_path
        except Exception as exc:  # best-effort artifact -- never propagate
            log.warning("live.debug_capture_failed", reason=reason, error=str(exc))
            return None

    # -- internals ------------------------------------------------------------------

    def _sidecar(
        self,
        *,
        reason: str,
        game_id: str,
        ply: int,
        ts: float,
        screenshot: str | None,
        screenshot_error: str | None,
        sample: Any,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "reason": reason,
            "game_id": game_id,
            "ply": ply,
            "ts": ts,
            "screenshot": screenshot,
            "screenshot_error": screenshot_error,
            "apk": _apk_sample_json(sample),
            "context": context,
        }

    def _safe_sample(self) -> Any:
        if self._apk_latest is None:
            return None
        try:
            return self._apk_latest()
        except Exception as exc:  # a flaky link must not block the capture
            log.debug("live.debug_capture_apk_failed", error=str(exc))
            return None

    def _prune_locked(self) -> None:
        """Keep the newest :attr:`keep_files` files in :attr:`root` (by mtime)."""
        try:
            entries = [p for p in self.root.iterdir() if p.is_file()]
            excess = len(entries) - self.keep_files
            if excess <= 0:
                return

            def _key(path: Path) -> tuple[float, str]:
                try:
                    return (path.stat().st_mtime, path.name)
                except OSError:  # raced with another pruner
                    return (0.0, path.name)

            entries.sort(key=_key)
            for stale in entries[:excess]:
                # Raced / locked unlink -- the next capture prunes again.
                with contextlib.suppress(OSError):
                    stale.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("live.debug_capture_prune_failed", error=str(exc))

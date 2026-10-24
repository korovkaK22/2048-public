"""`TgNotifier`: queue, routing, WARN digest, reminders, worker loop (§2.2).

The class is assembled from three worker-thread mixins -- transport
(:mod:`.transport`), outbox/cursors (:mod:`.outbox`) and inbound commands
(:mod:`.commands`) -- plus the public API and the single daemon loop here.
``notify()`` only enqueues; everything that touches the network runs in the
worker, so a Telegram outage can never stall or crash the runner.
"""

from __future__ import annotations

import atexit
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from .commands import _CommandsMixin
from .config import (
    ACK_FLAG_PATH,
    DEFAULT_ENV_PATH,
    SENT_JOURNAL_PATH,
    TgConfig,
    _resolve_env_path,
)
from .fileio import _append_jsonl
from .formatting import _Item, _maybe_markdown, _utc_now_iso, format_item, is_action_required
from .outbox import _OutboxMixin
from .transport import _TransportMixin

log = structlog.get_logger(__name__)

_DEDUP_WINDOW_S = 60.0
_WARN_DIGEST_S = 600.0
_REMINDER_S = 600.0
_OUTBOX_FLUSH_S = 60.0
_POLL_GAP_S = 2.0

#: Incident-bundle screenshots in preference order (docs/ops-monitoring.md §4):
#: the incident-time frame first, the last healthy frame as the fallback.
_INCIDENT_PHOTOS: tuple[str, ...] = (
    "screenshot_after.png",
    "screenshot_last_parsed.png",
    "screenshot_before.png",
)
#: A FATAL chained into its ERROR's bundle has its own fresh frame.
_INCIDENT_PHOTOS_FATAL: tuple[str, ...] = ("screenshot_fatal.png", *_INCIDENT_PHOTOS)


def incident_photo(root: Path, incident_dir: str | None, level: str = "") -> str | None:
    """Path of the photo to attach for an alert pointing at ``incident_dir``.

    ``None`` when the alert has no bundle or the bundle holds no screenshot
    (FLAG_SECURE-blind auth wall, device lost before any frame) -- the text
    alert then goes out alone. Shared by the in-process notifier hook and the
    reserve watcher so both pick the same picture.
    """
    if not incident_dir:
        return None
    names = _INCIDENT_PHOTOS_FATAL if level == "FATAL" else _INCIDENT_PHOTOS
    for name in names:
        cand = root / incident_dir / name
        if cand.is_file():
            return str(cand)
    return None


# ==============================================================================


class TgNotifier(_TransportMixin, _OutboxMixin, _CommandsMixin):
    """Single-daemon-thread Telegram notifier + command poller.

    ``notify()`` only enqueues (``try/except Exception`` around the whole
    body) -- a Telegram outage can NEVER stall or crash the runner. All
    HTTP, retries, routing, throttling and command handling live in the
    worker thread.

    Args:
        config: credentials; unusable config -> permanent no-op mode
            (single WARN in structlog, no secret values).
        root: repo root all relative paths hang off.
        api_base: override for tests (fake local Bot API server).
        screen_provider: callable returning PNG bytes for ``/screen``
            (wired to the device layer by the runner; None -> honest refusal).
        watch_mode: reserve-watcher flavour -- only /status and /help work.
    """

    def __init__(
        self,
        config: TgConfig | None = None,
        *,
        root: str | Path = ".",
        api_base: str = "https://api.telegram.org",
        screen_provider: Callable[[], bytes | None] | None = None,
        watch_mode: bool = False,
        retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0),
    ) -> None:
        self.config = config or TgConfig()
        self.root = Path(root)
        self.api_base = api_base.rstrip("/")
        self.screen_provider = screen_provider
        self.watch_mode = watch_mode
        self.retry_delays = retry_delays
        self._noop = not self.config.usable
        if self._noop:
            log.warning("tg.not_bootstrapped", reason="missing token/chat_id -> no-op")

        self._queue: queue.Queue[_Item] = queue.Queue(maxsize=200)
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

        # worker-side state (single thread => no locks needed)
        self._last_sent_mono = 0.0
        self._last_poll_mono = 0.0
        self._last_outbox_mono = 0.0
        self._last_digest_mono = time.monotonic()
        self._dedup: dict[tuple[str, str, int], tuple[float, int]] = {}
        self._warn_counts: dict[str, int] = {}
        self._warn_sent_once: set[str] = set()
        self._dropped_dupes = 0
        self._pending_action: dict[str, dict[str, Any]] = {}
        self._ignored_updates = 0
        self._session_end_sent = False
        self._session_started = False
        self._atexit_installed = False

    # -- public API -------------------------------------------------------------

    @classmethod
    def from_env(cls, path: str | Path = DEFAULT_ENV_PATH, **kw: Any) -> TgNotifier:
        env_path = _resolve_env_path(path)
        root = kw.pop(
            "root", env_path.parent.parent if env_path.parent.name == ".secrets" else "."
        )
        return cls(TgConfig.from_env(env_path), root=root, **kw)

    def start(self) -> None:
        """Start the single daemon worker (idempotent)."""
        if self._thread is not None or self._noop:
            return
        self._compact_outbox()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="bot2048-tg", daemon=True)
        self._thread.start()
        log.info("tg.started", watch_mode=self.watch_mode)

    def notify(
        self,
        level: str,
        code: str,
        text: str,
        photo_path: str | None = None,
        *,
        alert_ts: str | None = None,
    ) -> None:
        """Fire-and-forget. Never blocks, never raises (contract §2.1)."""
        try:
            if self._noop:
                return
            item = _Item("msg", str(level), str(code), str(text), photo_path, alert_ts=alert_ts)
            self._enqueue(item)
        except Exception:
            pass

    def notify_alert(self, alert: Any) -> None:
        """Adapter for :class:`bot2048.runner.alerts.AlertWriter` hook.

        Picks the incident photo (:func:`incident_photo`: the incident-time
        frame of THIS alert -- ``screenshot_fatal.png`` for a chained FATAL,
        else ``screenshot_after.png``, fallback ``screenshot_before.png``) and
        forwards the alert ts so the worker can journal delivery into
        ``logs/tg_sent.jsonl`` (§6.2). The reason travels in the caption.
        """
        try:
            level = getattr(alert.level, "value", str(alert.level))
            code = getattr(alert.code, "value", str(alert.code))
            photo = incident_photo(self.root, alert.incident_dir, level)
            self.notify(level, code, alert.message, photo, alert_ts=alert.ts)
        except Exception:
            pass

    def session_start(self, run_id: str = "", strategy: str = "") -> None:
        """🚫-message right after ``logs/runner.lock`` is taken (§3.3)."""
        try:
            if self._session_started:
                return
            self._session_started = True
            self._session_end_sent = False
            detail = ", ".join(
                x
                for x in (
                    f"run_id={run_id}" if run_id else "",
                    f"стратегія {strategy}" if strategy else "",
                )
                if x
            )
            text = "🚫 Бот працює — НЕ чіпай телефон." + (f" {detail}." if detail else "")
            self._enqueue(_Item("raw", "INFO", "SESSION_START", text))
            if not self._atexit_installed:
                self._atexit_installed = True
                atexit.register(self._atexit_hook)
        except Exception:
            pass

    def session_end(self, ok: bool, reason: str = "") -> None:
        """✅/💀-message; guarded against double sends (try/finally + atexit)."""
        try:
            if self._session_end_sent or not self._session_started:
                return
            self._session_end_sent = True
            if ok:
                text = "✅ Сесія завершена — телефон вільний." + (f" {reason}" if reason else "")
            else:
                text = (
                    f"💀 Бот впав ({reason or 'CRASH'}) — телефон вільний, "
                    "але подивись останнє повідомлення."
                )
                if self._pending_action:
                    text += " ⚠️ ПОТРІБЕН ТИ"
            self._enqueue(_Item("raw", "INFO", "SESSION_END", text))
        except Exception:
            pass

    def stop(self, flush_timeout: float = 5.0) -> None:
        """Best-effort flush then stop; leftovers land in the outbox."""
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=flush_timeout)
            if self._thread.is_alive():
                log.warning("tg.stop_timeout", flush_timeout=flush_timeout)
            self._thread = None
        # anything the worker did not manage to send -> outbox
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._to_outbox(item, attempt=0)

    def _atexit_hook(self) -> None:
        self.session_end(ok=False, reason="atexit")
        self.stop(flush_timeout=5.0)

    # -- queue ------------------------------------------------------------------

    def _enqueue(self, item: _Item) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            if item.level == "INFO":  # INFO is dropped first (§4.2)
                log.warning("tg.queue_full_info_dropped", code=item.code)
            else:
                self._to_outbox(item, attempt=0)

    # -- routing / formatting -----------------------------------------------------

    @staticmethod
    def _is_action_required(level: str, code: str) -> bool:
        return is_action_required(level, code)

    def _format(self, item: _Item) -> str:
        return format_item(item)

    def _process_item(self, item: _Item) -> None:
        now = time.monotonic()
        # dedup 60 s window on (level, code, hash(text))
        key = (item.level, item.code, hash(item.text))
        seen = self._dedup.get(key)
        if seen and now - seen[0] < _DEDUP_WINDOW_S:
            self._dedup[key] = (seen[0], seen[1] + 1)
            self._dropped_dupes += 1
            return
        self._dedup[key] = (now, 0)
        # WARN throttle: first of a code goes out, repeats feed the digest
        if item.level == "WARN" and item.kind != "raw":
            if item.code in self._warn_sent_once:
                self._warn_counts[item.code] = self._warn_counts.get(item.code, 0) + 1
                return
            self._warn_sent_once.add(item.code)
        text, parse_mode = _maybe_markdown(self._format(item))
        send_item = _Item(
            item.kind, item.level, item.code, text,
            item.photo_path, item.photo_bytes, item.alert_ts, item.ts, parse_mode,
        )
        ok = self._send_with_retry(send_item)
        if ok and item.alert_ts and item.level in ("ERROR", "FATAL"):
            _append_jsonl(
                self.root / SENT_JOURNAL_PATH,
                {"ts": item.alert_ts, "code": item.code, "sent_at": _utc_now_iso()},
            )
        if ok and self._is_action_required(item.level, item.code):
            self._pending_action[item.code] = {
                "text": text,
                "photo_path": item.photo_path,
                "first_mono": now,
                "last_sent_mono": now,
                "first_ts": time.time(),
            }

    # -- WARN digest / reminders ---------------------------------------------------

    def _maybe_warn_digest(self) -> None:
        now = time.monotonic()
        if now - self._last_digest_mono < _WARN_DIGEST_S:
            return
        self._last_digest_mono = now
        total = sum(self._warn_counts.values()) + self._dropped_dupes
        if total == 0:
            self._warn_sent_once.clear()
            return
        parts = [f"{code} ×{n}" for code, n in sorted(self._warn_counts.items()) if n]
        if self._dropped_dupes:
            parts.append(f"дублі ×{self._dropped_dupes}")
        text = f"🟡 WARN ×{total}: " + ", ".join(parts)
        self._warn_counts.clear()
        self._warn_sent_once.clear()
        self._dropped_dupes = 0
        self._send_with_retry(_Item("raw", "WARN", "WARN_DIGEST", text))

    def _acked_since(self, first_ts: float) -> bool:
        try:
            return (self.root / ACK_FLAG_PATH).stat().st_mtime >= first_ts
        except OSError:
            return False

    def _maybe_remind(self) -> None:
        if not self._pending_action:
            return
        now = time.monotonic()
        for code in list(self._pending_action):
            info = self._pending_action[code]
            if self._acked_since(info["first_ts"]):
                del self._pending_action[code]
                continue
            if now - info["last_sent_mono"] >= _REMINDER_S:
                info["last_sent_mono"] = now
                self._send_with_retry(_Item("raw", "FATAL", code, info["text"]))

    # -- worker loop (§2.2) ------------------------------------------------------------

    def _run(self) -> None:
        while True:
            if self._stop_evt.is_set() and self._queue.empty():
                break
            try:
                item: _Item | None = self._queue.get(timeout=0.2)
            except queue.Empty:
                item = None
            try:
                if item is not None:
                    self._process_item(item)
                    continue
                if self._stop_evt.is_set():
                    continue
                now = time.monotonic()
                if now - self._last_poll_mono >= _POLL_GAP_S:
                    self._last_poll_mono = now
                    self._poll_updates()
                if now - self._last_outbox_mono >= _OUTBOX_FLUSH_S:
                    self._last_outbox_mono = now
                    self._flush_outbox()
                self._maybe_warn_digest()
                self._maybe_remind()
            except Exception as exc:
                log.warning("TG_SEND_FAILED", error=type(exc).__name__)


# -- module singleton (runner side) ------------------------------------------------


_SINGLETON: TgNotifier | None = None
_SINGLETON_LOCK = threading.Lock()


def get_notifier() -> TgNotifier:
    """Lazy process-wide singleton built from ``.secrets/telegram.env``."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = TgNotifier(TgConfig.from_env())
        return _SINGLETON

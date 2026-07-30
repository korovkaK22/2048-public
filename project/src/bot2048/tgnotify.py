"""Telegram bridge: user notifications + inbound commands (docs/telegram-notify.md).

Main path: the runner calls :func:`TgNotifier.notify` (fire-and-forget, never
blocks, never raises) and a single daemon worker thread drains the queue via
the Bot API (stdlib ``urllib`` only -- ``requests`` is deliberately absent
from pyproject). Reserve path: ``python -m bot2048.tgnotify watch`` tails
``logs/alerts.jsonl`` with its OWN cursor and watches the heartbeat, for the
case when the runner (and its thread) died.

SECURITY (docs/telegram-notify.md §8): the token lives only inside
:class:`TgConfig`; ``repr`` masks it; the request URL is a local variable and
every ``urllib`` exception is re-packaged into :class:`TgHttpError` /
:class:`TgNetworkError` WITHOUT the URL (``HTTPError`` embeds it). Logs carry
only ``method``, ``status`` and the error class.

Files (relative to the repo root, see ops-monitoring.md):

===============================  ============================================
``logs/tg_outbox.jsonl``         offline outbox (append-only JSONL, fsync)
``logs/tg_outbox.cursor``        lines of the outbox already re-sent
``logs/tg_updates.offset``       getUpdates offset (atomic replace)
``logs/tg_sent.jsonl``           journal of delivered alerts (dedup vs watch)
``logs/tg_alerts.cursor``        watch-mode cursor over alerts.jsonl
``logs/tg_ack.flag``             /ack -- mutes the 10-min reminders
``control/pause.flag``           /pause -- the live loop parks between moves
===============================  ============================================
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

__all__ = [
    "ACTION_REQUIRED",
    "PAUSE_FLAG_PATH",
    "TgConfig",
    "TgError",
    "TgHttpError",
    "TgNetworkError",
    "TgNotifier",
    "get_notifier",
]

# -- paths (relative to repo root) -------------------------------------------

DEFAULT_ENV_PATH = Path(".secrets") / "telegram.env"
OUTBOX_PATH = Path("logs") / "tg_outbox.jsonl"
OUTBOX_CURSOR_PATH = Path("logs") / "tg_outbox.cursor"
UPDATES_OFFSET_PATH = Path("logs") / "tg_updates.offset"
SENT_JOURNAL_PATH = Path("logs") / "tg_sent.jsonl"
ALERTS_CURSOR_PATH = Path("logs") / "tg_alerts.cursor"
ACK_FLAG_PATH = Path("logs") / "tg_ack.flag"
PAUSE_FLAG_PATH = Path("control") / "pause.flag"
ALERTS_PATH = Path("logs") / "alerts.jsonl"
HEARTBEAT_PATH = Path("logs") / "heartbeat.json"

# -- routing constants (docs/telegram-notify.md §3) ---------------------------

#: codes whose FATAL/ERROR message opens with «⚠️ ПОТРІБЕН ТИ».
#: DEVICE_LOST is action-required only at FATAL level (§3.1).
ACTION_REQUIRED: frozenset[str] = frozenset(
    {"AUTH_REQUIRED", "RESTART_LOOP", "WATCHDOG_TIMEOUT"}
)

_ACTION_HINTS: dict[str, str] = {
    "AUTH_REQUIRED": "введи ПІН у monobank, потім /resume",
    "RESTART_LOOP": "бот у циклі рестартів — подивись incident-бандл, потім /resume",
    "WATCHDOG_TIMEOUT": "бот завис — перезапусти сесію або подивись логи",
    "DEVICE_LOST": "перевір USB/телефон, потім /resume",
}

_LEVEL_EMOJI = {"INFO": "ℹ️", "WARN": "🟡", "ERROR": "🔴", "FATAL": "💀"}

_TEXT_LIMIT = 4096
_CAPTION_LIMIT = 1024
_DEDUP_WINDOW_S = 60.0
_RATE_LIMIT_S = 1.05
_WARN_DIGEST_S = 600.0
_REMINDER_S = 600.0
_OUTBOX_FLUSH_S = 60.0
_POLL_GAP_S = 2.0
_CMD_MAX_AGE_S = 300.0
_WATCH_GRACE_S = 15.0
_HEARTBEAT_STALE_S = 60.0
_OUTBOX_DIGEST_AGE_S = 24 * 3600.0


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_KYIV_TZ: Any = None


def _kyiv_clock() -> str:
    """``14:03`` in Europe/Kyiv; fixed UTC+3 fallback when tzdata is absent."""
    global _KYIV_TZ
    if _KYIV_TZ is None:
        try:
            from zoneinfo import ZoneInfo

            _KYIV_TZ = ZoneInfo("Europe/Kyiv")
        except Exception:  # no tzdata on this Windows -- close enough
            _KYIV_TZ = timezone(timedelta(hours=3))
    return datetime.now(_KYIV_TZ).strftime("%H:%M")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# -- errors (never contain the URL/token) -------------------------------------


class TgError(Exception):
    """Base for transport failures; message never includes URL or token."""


class TgHttpError(TgError):
    """Bot API returned an HTTP error or ``ok=false``."""

    def __init__(
        self, status: int, description: str = "", retry_after: float | None = None
    ) -> None:
        super().__init__(f"telegram http {status}: {description[:200]}")
        self.status = status
        self.description = description
        self.retry_after = retry_after


class TgNetworkError(TgError):
    """DNS/socket/timeout failure; carries only the exception class name."""


# -- config -------------------------------------------------------------------


def _parse_env_file(path: Path) -> dict[str, str]:
    """Tiny ``KEY=VALUE`` parser (no python-dotenv). Missing file -> ``{}``."""
    out: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        out[key.strip()] = value
    return out


def _resolve_env_path(path: str | Path) -> Path:
    """Resolve a relative env path against the repo root when needed.

    Explicit paths keep their old meaning: absolute paths and relative
    paths that exist from the current CWD are returned untouched. Only
    when a relative path (e.g. the default ``.secrets/telegram.env``)
    does NOT exist as-is do we walk up from the CWD and anchor it at the
    repo root.

    An ancestor that CONTAINS a ``.secrets/`` directory always wins: on
    this layout the secrets live in the OUTER folder while the ``.git``
    marker sits in the inner ``project/`` repo one level below, so a
    ``.git`` match must never shadow a ``.secrets/`` ancestor higher up.
    We therefore make two passes: first find the nearest ancestor with a
    ``.secrets/`` dir; only if none has one do we fall back to the
    nearest ``.git`` marker. Nothing found -> the path is returned as
    given (old behaviour).
    """
    p = Path(path)
    try:
        if p.is_absolute() or p.exists():
            return p
        cwd = Path.cwd()
        ancestors = (cwd, *cwd.parents)
        # Pass 1: prefer the nearest ancestor that actually holds .secrets/.
        for ancestor in ancestors:
            if (ancestor / ".secrets").is_dir():
                log.debug("tg.env_path_resolved", root=str(ancestor), by="secrets")
                return ancestor / p
        # Pass 2: fall back to the nearest repo marker (CWD may itself be it).
        for ancestor in ancestors:
            if (ancestor / ".git").exists():
                log.debug("tg.env_path_resolved", root=str(ancestor), by="git")
                return ancestor / p
    except OSError:  # deleted CWD and friends -- fall through, never raise
        pass
    return p


@dataclass(frozen=True)
class TgConfig:
    """Bot credentials. ``repr``/``str`` mask the token."""

    token: str = ""
    chat_id: str = ""
    user_handle: str = ""

    def __repr__(self) -> str:
        return (
            f"TgConfig(token={'***' if self.token else ''!r}, "
            f"chat_id={self.chat_id!r}, user_handle={self.user_handle!r})"
        )

    __str__ = __repr__

    @property
    def usable(self) -> bool:
        return bool(self.token and self.chat_id)

    @classmethod
    def from_env(cls, path: str | Path = DEFAULT_ENV_PATH) -> TgConfig:
        """File first, ``os.environ`` as fallback per key. Never raises.

        A relative ``path`` that does not exist from the CWD is anchored
        at the nearest ancestor repo root (see :func:`_resolve_env_path`).
        """
        data = _parse_env_file(_resolve_env_path(path))

        def get(key: str) -> str:
            return data.get(key) or os.environ.get(key, "")

        return cls(
            token=get("TG_BOT_TOKEN"),
            chat_id=get("TG_CHAT_ID"),
            user_handle=get("TG_USER_HANDLE").lstrip("@"),
        )


# -- queue items ---------------------------------------------------------------


@dataclass(slots=True)
class _Item:
    kind: str  # "msg" | "raw" | "photo_bytes"
    level: str
    code: str
    text: str
    photo_path: str | None = None
    photo_bytes: bytes | None = None
    alert_ts: str | None = None  # ts of the alerts.jsonl row (tg_sent journal)
    ts: str = field(default_factory=_utc_now_iso)
    parse_mode: str | None = None  # "MarkdownV2" when the text carries a code block


#: Telegram MarkdownV2 reserved characters that must be backslash-escaped in
#: ordinary text (outside code blocks). https://core.telegram.org/bots/api#markdownv2-style
_MDV2_SPECIAL: frozenset[str] = frozenset(r"_*[]()~`>#+-=|{}.!")


def _escape_mdv2(text: str) -> str:
    """Backslash-escape every MarkdownV2-reserved char in ordinary text."""
    return "".join("\\" + ch if ch in _MDV2_SPECIAL else ch for ch in text)


def _maybe_markdown(text: str) -> tuple[str, str | None]:
    """Render ```` ```code``` ```` fences as Telegram MarkdownV2 code blocks.

    Messages WITHOUT a fence are returned verbatim with ``parse_mode=None`` --
    so every existing plain-text alert is byte-for-byte unchanged (no escaping,
    no risk). When a fence IS present the message switches to MarkdownV2: the
    ordinary segments are fully escaped (so no reserved char breaks parsing and
    NO HTML tags are ever emitted) and each fenced segment becomes a monospace
    ```` ``` ```` code block, so the final board grid lines up in Telegram.
    Inside a code block only ``\\`` and `` ` `` are special, so the grid (digits,
    spaces, dots) travels untouched. Unbalanced fences fall back to plain text
    rather than risk broken markup (which the Bot API would reject).
    """
    if "```" not in text:
        return text, None
    parts = text.split("```")
    if len(parts) % 2 == 0:  # odd number of fences -> unbalanced, do not risk markup
        return text, None
    rendered: list[str] = []
    for i, part in enumerate(parts):
        if i % 2:  # fenced segment -> monospace code block
            code = part.strip("\n").replace("\\", "\\\\").replace("`", "\\`")
            rendered.append(f"```\n{code}\n```")
        else:  # ordinary text -> escape every MarkdownV2 reserved char
            rendered.append(_escape_mdv2(part))
    return "".join(rendered), "MarkdownV2"


# -- append-only JSONL writer (same discipline as alerts.jsonl) ----------------


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ==============================================================================


class TgNotifier:
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

        Picks the incident photo (``screenshot_after.png``, fallback
        ``screenshot_before.png``) and forwards the alert ts so the worker
        can journal delivery into ``logs/tg_sent.jsonl`` (§6.2).
        """
        try:
            level = getattr(alert.level, "value", str(alert.level))
            code = getattr(alert.code, "value", str(alert.code))
            photo: str | None = None
            if alert.incident_dir:
                for name in ("screenshot_after.png", "screenshot_before.png"):
                    cand = self.root / alert.incident_dir / name
                    if cand.is_file():
                        photo = str(cand)
                        break
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

    # -- transport (worker thread only) ------------------------------------------

    def _api_call(
        self,
        method: str,
        params: dict[str, Any],
        photo: tuple[str, bytes] | None = None,
        timeout: float = 5.0,
    ) -> Any:
        """One Bot API call. The URL exists ONLY here (token safety §8)."""
        url = f"{self.api_base}/bot{self.config.token}/{method}"
        try:
            if photo is None:
                data = urllib.parse.urlencode(params).encode("utf-8")
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            else:
                boundary = uuid.uuid4().hex
                body = b""
                for key, value in params.items():
                    body += (
                        f"--{boundary}\r\nContent-Disposition: form-data; "
                        f'name="{key}"\r\n\r\n{value}\r\n'
                    ).encode()
                fname, blob = photo
                body += (
                    f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="photo"; filename="{fname}"\r\n'
                    "Content-Type: image/png\r\n\r\n"
                ).encode() + blob + f"\r\n--{boundary}--\r\n".encode()
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTPError carries the full URL (token!) -- repackage, chain cut.
            try:
                raw = exc.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            desc, retry_after = "", None
            try:
                j = json.loads(raw)
                desc = str(j.get("description", ""))
                ra = j.get("parameters", {}).get("retry_after")
                retry_after = float(ra) if ra is not None else None
            except Exception:
                pass
            raise TgHttpError(exc.code, desc, retry_after) from None
        except urllib.error.URLError as exc:
            raise TgNetworkError(type(exc.reason).__name__ if exc.reason else "URLError") from None
        except OSError as exc:
            raise TgNetworkError(type(exc).__name__) from None
        except ValueError as exc:
            # urlopen raises ValueError("unknown url type: <full url>") on a
            # malformed api_base -- and that URL carries the token. Reduce it to
            # the type name and cut the chain, like the branches above.
            raise TgNetworkError(type(exc).__name__) from None
        if not isinstance(payload, dict) or not payload.get("ok", False):
            desc = (
                str(payload.get("description", "not ok"))
                if isinstance(payload, dict)
                else "bad payload"
            )
            raise TgHttpError(0, desc)
        return payload.get("result")

    def _rate_limit(self) -> None:
        delta = time.monotonic() - self._last_sent_mono
        if delta < _RATE_LIMIT_S:
            time.sleep(_RATE_LIMIT_S - delta)

    def _send_once(self, item: _Item) -> None:
        """One send attempt (rate-limited); raises TgError on failure."""
        self._rate_limit()
        try:
            if item.kind == "photo_bytes" and item.photo_bytes:
                self._api_call(
                    "sendPhoto",
                    {
                        "chat_id": self.config.chat_id,
                        "caption": _truncate(item.text, _CAPTION_LIMIT),
                    },
                    photo=("screen.png", item.photo_bytes),
                )
            elif item.photo_path and Path(item.photo_path).is_file():
                blob = Path(item.photo_path).read_bytes()
                caption: dict[str, Any] = {
                    "chat_id": self.config.chat_id,
                    "caption": _truncate(item.text, _CAPTION_LIMIT),
                }
                if item.parse_mode:
                    caption["parse_mode"] = item.parse_mode
                self._api_call(
                    "sendPhoto",
                    caption,
                    photo=(Path(item.photo_path).name, blob),
                )
            else:
                message: dict[str, Any] = {
                    "chat_id": self.config.chat_id,
                    "text": _truncate(item.text, _TEXT_LIMIT),
                }
                if item.parse_mode:
                    message["parse_mode"] = item.parse_mode
                self._api_call("sendMessage", message)
        finally:
            self._last_sent_mono = time.monotonic()

    def _send_with_retry(self, item: _Item) -> bool:
        """Retry 1/2/4 s; 429 sleeps ``retry_after`` (cap 30 s) for free (§2.3).

        When stop is pending, a single attempt only -- the rest goes to the
        outbox so ``stop(flush_timeout=5)`` can actually finish in time.
        """
        throttled = 0
        attempt = 0
        max_attempts = 1 if self._stop_evt.is_set() else len(self.retry_delays)
        while True:
            try:
                self._send_once(item)
                return True
            except TgHttpError as exc:
                if exc.status == 429 and throttled < 5:
                    throttled += 1
                    time.sleep(min(exc.retry_after or 1.0, 30.0))
                    continue
                err: TgError = exc
            except TgNetworkError as exc:
                err = exc
            attempt += 1
            log.warning("TG_SEND_FAILED", error=type(err).__name__, attempt=attempt, code=item.code)
            if attempt >= max_attempts:
                self._to_outbox(item, attempt=attempt)
                return False
            time.sleep(self.retry_delays[attempt - 1])

    # -- routing / formatting -----------------------------------------------------

    @staticmethod
    def _is_action_required(level: str, code: str) -> bool:
        if code in ACTION_REQUIRED:
            return True
        return code == "DEVICE_LOST" and level == "FATAL"  # DEVICE_LOST(FATAL)

    def _format(self, item: _Item) -> str:
        if item.kind == "raw":
            return item.text
        lines = [f"{_LEVEL_EMOJI.get(item.level, 'ℹ️')} {item.level} {item.code}"]
        if self._is_action_required(item.level, item.code):
            hint = _ACTION_HINTS.get(item.code, "подивись останній алерт")
            lines.append(f"⚠️ ПОТРІБЕН ТИ — {hint}")
        lines.append(item.text)
        lines.append(f"🕐 {_kyiv_clock()} (Kyiv)")
        return "\n".join(lines)

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

    # -- outbox (§2.4) ---------------------------------------------------------------

    def _to_outbox(self, item: _Item, attempt: int) -> None:
        try:
            _append_jsonl(
                self.root / OUTBOX_PATH,
                {
                    "ts": item.ts,
                    "kind": "photo" if (item.photo_path or item.photo_bytes) else "msg",
                    "level": item.level,
                    "code": item.code,
                    "text": item.text,
                    "photo_path": item.photo_path,
                    "alert_ts": item.alert_ts,
                    "attempt": attempt,
                },
            )
        except OSError as exc:
            log.warning("tg.outbox_write_failed", error=type(exc).__name__)

    def _read_cursor(self, path: Path) -> int:
        try:
            return int((self.root / path).read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    def _write_cursor(self, path: Path, value: int) -> None:
        _atomic_write_text(self.root / path, str(value))

    def _compact_outbox(self) -> None:
        """Startup-only: drop already-sent lines, reset the cursor (§2.4)."""
        outbox = self.root / OUTBOX_PATH
        sent = self._read_cursor(OUTBOX_CURSOR_PATH)
        if sent <= 0 or not outbox.is_file():
            return
        try:
            lines = outbox.read_text(encoding="utf-8").splitlines()
            rest = lines[sent:]
            _atomic_write_text(outbox, "".join(line + "\n" for line in rest))
            self._write_cursor(OUTBOX_CURSOR_PATH, 0)
            log.info("tg.outbox_compacted", dropped=min(sent, len(lines)), left=len(rest))
        except OSError as exc:
            log.warning("tg.outbox_compact_failed", error=type(exc).__name__)

    def _flush_outbox(self) -> None:
        outbox = self.root / OUTBOX_PATH
        if not outbox.is_file():
            return
        cursor = self._read_cursor(OUTBOX_CURSOR_PATH)
        try:
            lines = outbox.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        stale: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for idx in range(cursor, len(lines)):
            try:
                row = json.loads(lines[idx])
            except ValueError:
                cursor = idx + 1  # broken line: skip forever
                continue
            try:
                ts = datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
                age = (now - ts).total_seconds()
            except (KeyError, ValueError):
                age = 0.0
            if age > _OUTBOX_DIGEST_AGE_S:
                stale.append(row)
                cursor = idx + 1
                continue
            item = _Item(
                "msg", row.get("level", "INFO"), row.get("code", ""), row.get("text", ""),
                row.get("photo_path"), alert_ts=row.get("alert_ts"),
            )
            try:
                self._send_once(item)
            except TgError:
                break  # network still down -- next round in 60 s
            cursor = idx + 1
        if stale:
            digest = (
                f"⏳ поки не було мережі: {len(stale)} повідомлень; "
                f"останнє: {stale[-1].get('text', '')[:200]}"
            )
            with contextlib.suppress(TgError):
                self._send_once(_Item("raw", "INFO", "OUTBOX_DIGEST", digest))
        self._write_cursor(OUTBOX_CURSOR_PATH, cursor)

    # -- inbound commands (§5) ----------------------------------------------------------

    def _poll_updates(self) -> None:
        offset = self._read_cursor(UPDATES_OFFSET_PATH)
        try:
            result = self._api_call(
                "getUpdates",
                {"offset": offset + 1, "timeout": 2, "limit": 10,
                 "allowed_updates": '["message"]'},
            )
        except TgError as exc:
            log.warning("tg.poll_failed", error=type(exc).__name__)
            return
        if not result:
            return
        max_id = offset
        for update in result:
            max_id = max(max_id, int(update.get("update_id", 0)))
            msg = update.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if chat_id != str(self.config.chat_id):
                self._ignored_updates += 1  # foreign chat: silent (§5)
                continue
            if time.time() - float(msg.get("date", 0)) > _CMD_MAX_AGE_S:
                continue  # stale command from Telegram's own outbox
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                try:
                    self._handle_command(text.split()[0].split("@")[0].lower())
                except Exception as exc:
                    log.warning("tg.command_failed", error=type(exc).__name__)
        self._write_cursor(UPDATES_OFFSET_PATH, max_id)

    def _reply(self, text: str) -> None:
        try:
            self._send_once(_Item("raw", "INFO", "REPLY", _truncate(text, _TEXT_LIMIT)))
        except TgError as exc:
            log.warning("TG_SEND_FAILED", error=type(exc).__name__, code="REPLY")

    def _handle_command(self, cmd: str) -> None:
        if self.watch_mode and cmd not in ("/status", "/help"):
            self._reply("раннер не запущений — доступні лише /status і /help")
            return
        if cmd == "/help":
            self._reply(
                "/status — стан бота\n/screen — свіжий скріншот\n"
                "/pause — пауза після поточного ходу\n/resume — продовжити\n"
                "/ack — підтвердити алерт (глушить нагадування)\n/help — це меню"
            )
        elif cmd == "/status":
            self._reply(self._status_text())
        elif cmd == "/screen":
            self._cmd_screen()
        elif cmd == "/pause":
            flag = self.root / PAUSE_FLAG_PATH
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text(f"{_utc_now_iso()} tg:/pause\n", encoding="utf-8")
            self._reply("⏸ паузну після поточного ходу")
        elif cmd == "/resume":
            (self.root / PAUSE_FLAG_PATH).unlink(missing_ok=True)
            self._reply("▶️ продовжую")
        elif cmd == "/ack":
            ack = self.root / ACK_FLAG_PATH
            ack.parent.mkdir(parents=True, exist_ok=True)
            ack.write_text(f"{_utc_now_iso()} tg:/ack\n", encoding="utf-8")
            self._pending_action.clear()
            self._reply("👍 прийнято — нагадування вимкнено")
        # anything else: silence (not a broadcast bot)

    def _status_text(self) -> str:
        parts: list[str] = []
        hb = self.root / HEARTBEAT_PATH
        try:
            data = json.loads(hb.read_text(encoding="utf-8"))
            age = time.time() - hb.stat().st_mtime
            state = "живий" if age < _HEARTBEAT_STALE_S else f"⚠️ heartbeat протух ({int(age)} с)"
            parts.append(
                f"{state} · run_id={data.get('run_id')} · гра={data.get('game_id')}"
                f" · хід={data.get('ply')}"
            )
        except (OSError, ValueError):
            parts.append("heartbeat нема — раннер не запущений або впав")
        if (self.root / PAUSE_FLAG_PATH).is_file():
            parts.append("⏸ пауза активна (control/pause.flag)")
        last = self._last_alert_line()
        if last:
            parts.append(f"останній алерт: {last}")
        return "\n".join(parts)

    def _last_alert_line(self) -> str:
        path = self.root / ALERTS_PATH
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 4096))
                tail = fh.read().decode("utf-8", "replace").strip().splitlines()
            if not tail:
                return ""
            row = json.loads(tail[-1])
            return f"{row.get('level')} {row.get('code')} @ {row.get('ts')}"
        except (OSError, ValueError):
            return ""

    def _cmd_screen(self) -> None:
        if self.screen_provider is None:
            self._reply("скрін недоступний у цьому режимі")
            return
        try:
            png = self.screen_provider()
        except Exception as exc:
            self._reply(f"не зміг зняти скрін: {type(exc).__name__}")
            return
        if not png:
            self._reply("екран захищено FLAG_SECURE — мабуть, банк просить ПІН")
            return
        try:
            self._send_once(
                _Item(
                    "photo_bytes", "INFO", "SCREEN",
                    f"🕐 {_kyiv_clock()} (Kyiv)", photo_bytes=png,
                )
            )
        except TgError as exc:
            self._reply(f"не зміг відправити скрін: {type(exc).__name__}")

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


def __getattr__(name: str) -> Any:  # `from bot2048.tgnotify import tg`
    if name == "tg":
        return get_notifier()
    raise AttributeError(name)


# -- watch mode (§6.1) ---------------------------------------------------------------


def _load_sent_journal(root: Path) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    try:
        for line in (root / SENT_JOURNAL_PATH).read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                out.add((str(row.get("ts")), str(row.get("code"))))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _alert_age_s(ts: str) -> float:
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds()
    except ValueError:
        return float("inf")


def watch(
    root: str | Path = ".",
    *,
    once: bool = False,
    poll_s: float = 2.0,
    notifier: TgNotifier | None = None,
) -> None:
    """Reserve watcher: tail alerts.jsonl + heartbeat watchdog (§6.1).

    Uses its OWN cursor ``logs/tg_alerts.cursor`` (never touches the
    the runner's ``logs/alerts.cursor``); sends only ERROR/FATAL; dedups
    against ``logs/tg_sent.jsonl`` with a 15 s grace so the runner's own
    thread gets to deliver first. ``once=True`` -- one pass (tests);
    ``notifier`` -- injection point for tests.
    """
    root = Path(root)
    if notifier is None:
        notifier = TgNotifier(
            TgConfig.from_env(root / DEFAULT_ENV_PATH), root=root, watch_mode=True
        )
    notifier.start()
    hb_alerted = False
    log.info("tg.watch_started", root=str(root))
    while True:
        try:
            # 1) new alert lines past our cursor
            alerts_file = root / ALERTS_PATH
            cursor = notifier._read_cursor(ALERTS_CURSOR_PATH)
            lines: list[str] = []
            if alerts_file.is_file():
                lines = alerts_file.read_text(encoding="utf-8").splitlines()
            new_cursor = cursor
            for idx in range(cursor, len(lines)):
                try:
                    row = json.loads(lines[idx])
                except ValueError:
                    new_cursor = idx + 1
                    continue
                level, code, ts = row.get("level"), row.get("code"), row.get("ts", "")
                if level not in ("ERROR", "FATAL"):
                    new_cursor = idx + 1
                    continue
                if _alert_age_s(ts) < _WATCH_GRACE_S:
                    break  # grace: let the runner's thread go first; retry next pass
                if (ts, code) in _load_sent_journal(root):
                    new_cursor = idx + 1
                    continue
                photo = None
                if row.get("incident_dir"):
                    for name in ("screenshot_after.png", "screenshot_before.png"):
                        cand = root / row["incident_dir"] / name
                        if cand.is_file():
                            photo = str(cand)
                            break
                notifier.notify(level, code, row.get("message", ""), photo, alert_ts=ts)
                new_cursor = idx + 1
            if new_cursor != cursor:
                notifier._write_cursor(ALERTS_CURSOR_PATH, new_cursor)
            # 2) heartbeat watchdog: stale file (present!) => runner hangs
            hb = root / HEARTBEAT_PATH
            if hb.is_file():
                age = time.time() - hb.stat().st_mtime
                if age > _HEARTBEAT_STALE_S and not hb_alerted:
                    hb_alerted = True
                    notifier.notify(
                        "FATAL", "WATCHDOG_TIMEOUT",
                        f"💀 бот завис: heartbeat не оновлювався {int(age)} с"
                        " — телефон вільний, але бот завис.",
                    )
                elif age <= _HEARTBEAT_STALE_S:
                    hb_alerted = False
            else:
                hb_alerted = False  # clean exit removed the file
        except Exception as exc:
            log.warning("tg.watch_error", error=type(exc).__name__)
        if once:
            notifier.stop(flush_timeout=10.0)
            return
        time.sleep(poll_s)


# -- bootstrap (§7) --------------------------------------------------------------------


def bootstrap(root: str | Path = ".", *, timeout_s: float = 300.0) -> int:
    """One-time TG_CHAT_ID discovery; returns process exit code."""
    root = Path(root)
    env_path = root / DEFAULT_ENV_PATH
    cfg = TgConfig.from_env(env_path)
    if not cfg.token:
        print("немає TG_BOT_TOKEN у .secrets/telegram.env — спершу створи бота")
        return 1
    if cfg.chat_id:
        print(f"TG_CHAT_ID вже заповнено ({cfg.chat_id}) — пропускаю bootstrap")
        return 0
    want = cfg.user_handle.lstrip("@")
    notifier = TgNotifier(cfg, root=root)
    print(f"чекаю /start від @{want} у бота (до {int(timeout_s)} с)…")
    deadline = time.monotonic() + timeout_s
    offset = 0
    while time.monotonic() < deadline:
        try:
            result = notifier._api_call(
                "getUpdates", {"offset": offset + 1, "timeout": 25}, timeout=30.0
            )
        except TgError as exc:
            print(f"getUpdates збій: {type(exc).__name__}; повторюю…")
            time.sleep(3)
            continue
        for update in result or []:
            offset = max(offset, int(update.get("update_id", 0)))
            msg = update.get("message") or {}
            username = (msg.get("from") or {}).get("username", "")
            if username != want:  # never take a stranger's chat_id (§7.4)
                continue
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if not chat_id:
                continue
            _rewrite_env_key(env_path, "TG_CHAT_ID", chat_id)
            done = TgNotifier(TgConfig.from_env(env_path), root=root)
            try:
                done._send_once(_Item("raw", "INFO", "BOOTSTRAP", "✅ Міст налаштовано"))
            except TgError as exc:
                print(f"chat_id записано, але тест-повідомлення не пішло: {type(exc).__name__}")
                return 1
            print(f"TG_CHAT_ID={chat_id} записано, тест відправлено")
            return 0
    print(f"немає updates — потрібен /start від @{want}")
    return 1


def _rewrite_env_key(path: Path, key: str, value: str) -> None:
    """Replace/append ``key=value`` keeping every other line intact."""
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    hit = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            hit = True
    if not hit:
        lines.append(f"{key}={value}")
    _atomic_write_text(path, "".join(line + "\n" for line in lines))


# -- CLI: python -m bot2048.tgnotify {watch|bootstrap|send} -----------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="bot2048.tgnotify", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch", help="резервний вотчер alerts.jsonl + heartbeat")
    w.add_argument("--root", default=".", help="корінь repo (де logs/ і .secrets/)")
    b = sub.add_parser("bootstrap", help="одноразове визначення TG_CHAT_ID")
    b.add_argument("--root", default=".")
    s = sub.add_parser("send", help='ручна відправка: send "text" [photo]')
    s.add_argument("text")
    s.add_argument("photo", nargs="?", default=None)
    s.add_argument("--root", default=".")
    args = parser.parse_args(argv)

    if args.cmd == "watch":
        watch(args.root)
        return 0
    if args.cmd == "bootstrap":
        return bootstrap(args.root)
    # send
    cfg = TgConfig.from_env(Path(args.root) / DEFAULT_ENV_PATH)
    if not cfg.usable:
        print("tg не налаштовано (.secrets/telegram.env)")
        return 1
    notifier = TgNotifier(cfg, root=args.root)
    try:
        notifier._send_once(_Item("raw", "INFO", "MANUAL", args.text, photo_path=args.photo))
    except TgError as exc:
        print(f"збій відправки: {type(exc).__name__}")
        return 1
    print("відправлено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

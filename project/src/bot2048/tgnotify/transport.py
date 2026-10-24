"""Bot API transport: one HTTP call, rate limit, retry/backoff (§2.3, §4.2).

stdlib ``urllib`` only -- ``requests`` is deliberately absent from pyproject.

SECURITY (docs/telegram-notify.md §8): the request URL exists ONLY as a local
variable inside :meth:`_TransportMixin._api_call`; every ``urllib`` exception
is re-packaged into :class:`TgHttpError` / :class:`TgNetworkError` WITHOUT the
URL (``HTTPError`` embeds it) and with the chain cut. Logs carry only
``method``, ``status`` and the error class.

The mixin is worker-thread-only state shared with :class:`TgNotifier`; the
host contract it relies on is spelled out in :class:`_TransportHost`.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Protocol

import structlog

from .config import TgConfig
from .formatting import _CAPTION_LIMIT, _TEXT_LIMIT, _Item, _truncate

log = structlog.get_logger(__name__)

#: 1 msg/s per chat (§4.2) -- tests patch this to 0.
_RATE_LIMIT_S = 1.05


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


# -- mixin ----------------------------------------------------------------------


class _TransportHost(Protocol):
    """What :class:`_TransportMixin` needs from its host (:class:`TgNotifier`)."""

    config: TgConfig
    api_base: str
    retry_delays: tuple[float, ...]
    _last_sent_mono: float
    _stop_evt: threading.Event

    def _to_outbox(self, item: _Item, attempt: int) -> None: ...

    def _api_call(
        self,
        method: str,
        params: dict[str, Any],
        photo: tuple[str, bytes] | None = None,
        timeout: float = 5.0,
    ) -> Any: ...

    def _rate_limit(self) -> None: ...

    def _send_once(self, item: _Item) -> None: ...


class _TransportMixin:
    """HTTP layer of :class:`TgNotifier` (worker thread only)."""

    def _api_call(
        self: _TransportHost,
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

    def _rate_limit(self: _TransportHost) -> None:
        delta = time.monotonic() - self._last_sent_mono
        if delta < _RATE_LIMIT_S:
            time.sleep(_RATE_LIMIT_S - delta)

    def _send_once(self: _TransportHost, item: _Item) -> None:
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

    def _send_with_retry(self: _TransportHost, item: _Item) -> bool:
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

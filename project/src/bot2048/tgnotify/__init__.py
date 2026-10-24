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

Package layout (one concern per module; the public surface is re-exported here):

=================  ==========================================================
``config``         paths (``logs/tg_*``, ``control/pause.flag``), env file, ``TgConfig``
``formatting``     ``_Item``, §4.1 template, Kyiv clock, limits, MarkdownV2 fences
``transport``      Bot API HTTP call, errors, rate limit, retry/backoff
``outbox``         offline outbox + cursor files
``commands``       getUpdates poller and ``/status /screen /pause /resume /ack /help``
``notifier``       :class:`TgNotifier` (queue, routing, digest, reminders, worker loop)
``watch``          reserve watcher (alerts.jsonl tail + heartbeat watchdog)
``bootstrap``      one-time ``TG_CHAT_ID`` discovery
``cli``            ``python -m bot2048.tgnotify {watch|bootstrap|send}``
=================  ==========================================================
"""

from __future__ import annotations

from typing import Any

from .bootstrap import bootstrap as bootstrap
from .config import (
    ACK_FLAG_PATH as ACK_FLAG_PATH,
)
from .config import (
    ALERTS_CURSOR_PATH as ALERTS_CURSOR_PATH,
)
from .config import (
    ALERTS_PATH as ALERTS_PATH,
)
from .config import (
    DEFAULT_ENV_PATH as DEFAULT_ENV_PATH,
)
from .config import (
    HEARTBEAT_PATH as HEARTBEAT_PATH,
)
from .config import (
    OUTBOX_CURSOR_PATH as OUTBOX_CURSOR_PATH,
)
from .config import (
    OUTBOX_PATH as OUTBOX_PATH,
)
from .config import (
    PAUSE_FLAG_PATH,
    TgConfig,
)
from .config import (
    SENT_JOURNAL_PATH as SENT_JOURNAL_PATH,
)
from .config import (
    UPDATES_OFFSET_PATH as UPDATES_OFFSET_PATH,
)
from .config import (
    _parse_env_file as _parse_env_file,
)
from .config import (
    _resolve_env_path as _resolve_env_path,
)
from .formatting import (
    ACTION_REQUIRED,
)
from .formatting import (
    _escape_mdv2 as _escape_mdv2,
)
from .formatting import (
    _Item as _Item,
)
from .formatting import (
    _maybe_markdown as _maybe_markdown,
)
from .formatting import (
    _truncate as _truncate,
)
from .notifier import TgNotifier, get_notifier
from .transport import TgError, TgHttpError, TgNetworkError
from .watch import watch as watch

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


def __getattr__(name: str) -> Any:  # `from bot2048.tgnotify import tg` / `.main`
    if name == "tg":
        return get_notifier()
    if name == "main":
        from .cli import main

        return main
    raise AttributeError(name)

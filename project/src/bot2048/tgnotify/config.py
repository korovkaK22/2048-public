"""Credentials + file locations of the Telegram bridge.

Paths are relative to the repo root (see ops-monitoring.md):

===============================  ============================================
``logs/tg_outbox.jsonl``         offline outbox (append-only JSONL, fsync)
``logs/tg_outbox.cursor``        lines of the outbox already re-sent
``logs/tg_updates.offset``       getUpdates offset (atomic replace)
``logs/tg_sent.jsonl``           journal of delivered alerts (dedup vs watch)
``logs/tg_alerts.cursor``        watch-mode cursor over alerts.jsonl
``logs/tg_ack.flag``             /ack -- mutes the 10-min reminders
``control/pause.flag``           /pause -- the live loop parks between moves
===============================  ============================================

SECURITY (docs/telegram-notify.md §8): the token lives only inside
:class:`TgConfig`; ``repr`` masks it. Nothing here ever logs a secret value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import structlog

from .fileio import _atomic_write_text

log = structlog.get_logger(__name__)

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

#: ``logs/heartbeat.json`` older than this => the runner hangs (shared by
#: the ``/status`` reply and the watch-mode watchdog).
_HEARTBEAT_STALE_S = 60.0


# -- env file -----------------------------------------------------------------


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


# -- config -------------------------------------------------------------------


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

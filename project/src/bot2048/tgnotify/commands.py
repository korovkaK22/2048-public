"""Inbound commands via ``getUpdates`` (docs/telegram-notify.md §5).

``/status /screen /pause /resume /ack /help`` -- accepted ONLY from
``TG_CHAT_ID``; foreign chats are confirmed by offset and ignored silently
(counter only, never the text). Runs inside the worker thread between sends.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import structlog

from .config import (
    _HEARTBEAT_STALE_S,
    ACK_FLAG_PATH,
    ALERTS_PATH,
    HEARTBEAT_PATH,
    PAUSE_FLAG_PATH,
    UPDATES_OFFSET_PATH,
    TgConfig,
)
from .formatting import _TEXT_LIMIT, _Item, _kyiv_clock, _truncate, _utc_now_iso
from .transport import TgError

log = structlog.get_logger(__name__)

#: commands older than this (``message.date``) are dropped -- Telegram's own outbox
_CMD_MAX_AGE_S = 300.0


class _CommandsHost(Protocol):
    """What :class:`_CommandsMixin` needs from its host (:class:`TgNotifier`)."""

    root: Path
    config: TgConfig
    watch_mode: bool
    screen_provider: Callable[[], bytes | None] | None
    _ignored_updates: int
    _pending_action: dict[str, dict[str, Any]]

    def _api_call(
        self,
        method: str,
        params: dict[str, Any],
        photo: tuple[str, bytes] | None = None,
        timeout: float = 5.0,
    ) -> Any: ...

    def _send_once(self, item: _Item) -> None: ...

    def _read_cursor(self, path: Path) -> int: ...

    def _write_cursor(self, path: Path, value: int) -> None: ...

    def _reply(self, text: str) -> None: ...

    def _handle_command(self, cmd: str) -> None: ...

    def _status_text(self) -> str: ...

    def _last_alert_line(self) -> str: ...

    def _cmd_screen(self) -> None: ...


class _CommandsMixin:
    """Command poller/handler of :class:`TgNotifier` (worker thread only)."""

    def _poll_updates(self: _CommandsHost) -> None:
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

    def _reply(self: _CommandsHost, text: str) -> None:
        try:
            self._send_once(_Item("raw", "INFO", "REPLY", _truncate(text, _TEXT_LIMIT)))
        except TgError as exc:
            log.warning("TG_SEND_FAILED", error=type(exc).__name__, code="REPLY")

    def _handle_command(self: _CommandsHost, cmd: str) -> None:
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

    def _status_text(self: _CommandsHost) -> str:
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

    def _last_alert_line(self: _CommandsHost) -> str:
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

    def _cmd_screen(self: _CommandsHost) -> None:
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

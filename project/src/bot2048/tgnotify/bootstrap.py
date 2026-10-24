"""One-time ``TG_CHAT_ID`` discovery ``python -m bot2048.tgnotify bootstrap`` (§7).

Interactive CLI step, hence ``print`` for the operator; the token value is
never printed. Only the configured ``TG_USER_HANDLE`` may claim the chat_id.
"""

from __future__ import annotations

import time
from pathlib import Path

from .config import DEFAULT_ENV_PATH, TgConfig, _rewrite_env_key
from .formatting import _Item
from .notifier import TgNotifier
from .transport import TgError


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

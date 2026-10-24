"""Queue item + message rendering (docs/telegram-notify.md §3, §4).

Pure functions only: level emoji, «⚠️ ПОТРІБЕН ТИ» hints, Kyiv clock,
truncation to Telegram limits and the MarkdownV2 code-block rendering.
No I/O, no state -- safe to call from any thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

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

#: Telegram limits (§4.2): message text / sendPhoto caption.
_TEXT_LIMIT = 4096
_CAPTION_LIMIT = 1024


# -- time / text helpers -------------------------------------------------------


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


# -- MarkdownV2 code blocks ------------------------------------------------------

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


# -- routing / formatting (§3.1, §4.1) -------------------------------------------


def is_action_required(level: str, code: str) -> bool:
    if code in ACTION_REQUIRED:
        return True
    return code == "DEVICE_LOST" and level == "FATAL"  # DEVICE_LOST(FATAL)


def format_item(item: _Item) -> str:
    """§4.1 template; ``raw`` items (session/digest/replies) go out verbatim."""
    if item.kind == "raw":
        return item.text
    lines = [f"{_LEVEL_EMOJI.get(item.level, 'ℹ️')} {item.level} {item.code}"]
    if is_action_required(item.level, item.code):
        hint = _ACTION_HINTS.get(item.code, "подивись останній алерт")
        lines.append(f"⚠️ ПОТРІБЕН ТИ — {hint}")
    lines.append(item.text)
    lines.append(f"🕐 {_kyiv_clock()} (Kyiv)")
    return "\n".join(lines)

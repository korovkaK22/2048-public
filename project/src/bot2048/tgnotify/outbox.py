"""Offline outbox ``logs/tg_outbox.jsonl`` + cursor files (§2.4).

Append-only JSONL; the cursor (``logs/tg_outbox.cursor``) counts lines
already re-sent. Rewriting the file while running is forbidden -- compaction
happens only at :meth:`TgNotifier.start`. The cursor helpers are generic and
also serve the getUpdates offset and the watch-mode alerts cursor.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import structlog

from .config import OUTBOX_CURSOR_PATH, OUTBOX_PATH
from .fileio import _append_jsonl, _atomic_write_text
from .formatting import _Item
from .transport import TgError

log = structlog.get_logger(__name__)

#: outbox rows older than this are collapsed into one «⏳ поки не було мережі» digest
_OUTBOX_DIGEST_AGE_S = 24 * 3600.0


class _OutboxHost(Protocol):
    """What :class:`_OutboxMixin` needs from its host (:class:`TgNotifier`)."""

    root: Path

    def _send_once(self, item: _Item) -> None: ...

    def _read_cursor(self, path: Path) -> int: ...

    def _write_cursor(self, path: Path, value: int) -> None: ...


class _OutboxMixin:
    """Outbox + cursor persistence of :class:`TgNotifier`."""

    def _to_outbox(self: _OutboxHost, item: _Item, attempt: int) -> None:
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

    def _read_cursor(self: _OutboxHost, path: Path) -> int:
        try:
            return int((self.root / path).read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    def _write_cursor(self: _OutboxHost, path: Path, value: int) -> None:
        _atomic_write_text(self.root / path, str(value))

    def _compact_outbox(self: _OutboxHost) -> None:
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

    def _flush_outbox(self: _OutboxHost) -> None:
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

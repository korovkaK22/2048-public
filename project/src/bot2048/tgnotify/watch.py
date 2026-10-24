"""Reserve watcher ``python -m bot2048.tgnotify watch`` (§6.1).

For the case when the runner (and its notifier thread) died: tails
``logs/alerts.jsonl`` with its OWN cursor and watches the heartbeat.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog

from .config import (
    _HEARTBEAT_STALE_S,
    ALERTS_CURSOR_PATH,
    ALERTS_PATH,
    DEFAULT_ENV_PATH,
    HEARTBEAT_PATH,
    SENT_JOURNAL_PATH,
    TgConfig,
)
from .notifier import TgNotifier, incident_photo

log = structlog.get_logger(__name__)

#: let the runner's own thread deliver first; the watcher waits this long
_WATCH_GRACE_S = 15.0


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
                photo = incident_photo(root, row.get("incident_dir"), str(level))
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

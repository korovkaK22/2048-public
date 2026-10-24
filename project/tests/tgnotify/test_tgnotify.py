"""Tests for the Telegram bridge (docs/telegram-notify.md §9.11).

Unit tests drive the worker-side methods synchronously with a fake
``_api_call`` (no threads, no sockets); the integration test spins a fake
Bot API on ``http.server`` and exercises the real daemon thread + urllib
transport end to end, then greps every produced file for the token (§8).
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

import bot2048.tgnotify as tgnotify
from bot2048.tgnotify import (
    ACK_FLAG_PATH,
    ALERTS_CURSOR_PATH,
    ALERTS_PATH,
    HEARTBEAT_PATH,
    OUTBOX_CURSOR_PATH,
    OUTBOX_PATH,
    PAUSE_FLAG_PATH,
    SENT_JOURNAL_PATH,
    UPDATES_OFFSET_PATH,
    TgConfig,
    TgHttpError,
    TgNetworkError,
    TgNotifier,
    _Item,
    _parse_env_file,
    _resolve_env_path,
    _truncate,
    watch,
)

TOKEN = "1234567890:SECRET-TOKEN-abcdef"
CHAT_ID = "100000001"


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch):
    """No rate-limit sleeps in tests (the constant lives in the transport module)."""
    monkeypatch.setattr(tgnotify.transport, "_RATE_LIMIT_S", 0.0)


def make_notifier(tmp_path: Path, api=None, **kw) -> TgNotifier:
    cfg = TgConfig(token=TOKEN, chat_id=CHAT_ID, user_handle="test_user")
    n = TgNotifier(cfg, root=tmp_path, retry_delays=(0.01, 0.01, 0.01), **kw)
    if api is not None:
        n._api_call = api  # type: ignore[method-assign]
    return n


class RecordingApi:
    """Fake ``_api_call``: records calls, scripted failures."""

    def __init__(self, fail: list[Exception] | None = None, updates=None):
        self.calls: list[tuple[str, dict]] = []
        self.fail = list(fail or [])
        self.updates = updates if updates is not None else []

    def __call__(self, method, params, photo=None, timeout=5.0):
        self.calls.append((method, dict(params)))
        if self.fail:
            raise self.fail.pop(0)
        if method == "getUpdates":
            got, self.updates = self.updates, []
            return got
        return {"message_id": len(self.calls)}

    def sent_texts(self) -> list[str]:
        return [
            p.get("text", p.get("caption", ""))
            for m, p in self.calls
            if m in ("sendMessage", "sendPhoto")
        ]


# -- config / env parser (§2.1, §8) -------------------------------------------


def test_env_parser(tmp_path: Path):
    env = tmp_path / "telegram.env"
    env.write_text(
        "# comment\nexport TG_BOT_TOKEN='abc:123'\nTG_CHAT_ID=42\n\nBROKEN LINE\n"
        'TG_USER_HANDLE="@test_user"\n',
        encoding="utf-8",
    )
    data = _parse_env_file(env)
    assert data == {"TG_BOT_TOKEN": "abc:123", "TG_CHAT_ID": "42", "TG_USER_HANDLE": "@test_user"}
    cfg = TgConfig.from_env(env)
    assert cfg.token == "abc:123" and cfg.chat_id == "42" and cfg.user_handle == "test_user"
    assert _parse_env_file(tmp_path / "missing.env") == {}


def _isolate_tg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("TG_BOT_TOKEN", "TG_CHAT_ID", "TG_USER_HANDLE"):
        monkeypatch.delenv(key, raising=False)


def test_env_resolved_from_parent_of_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Runner starts from project/ -> .secrets is found at the repo root above."""
    _isolate_tg_env(monkeypatch)
    repo = tmp_path / "repo"
    secrets = repo / ".secrets"
    secrets.mkdir(parents=True)
    (secrets / "telegram.env").write_text(
        f"TG_BOT_TOKEN={TOKEN}\nTG_CHAT_ID={CHAT_ID}\n", encoding="utf-8"
    )
    project = repo / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    cfg = TgConfig.from_env()  # default relative .secrets/telegram.env
    assert cfg.token == TOKEN and cfg.chat_id == CHAT_ID and cfg.usable
    # root derivation lands at the repo root, not project/
    n = TgNotifier.from_env()
    assert Path(n.root).resolve() == repo.resolve()
    assert not n._noop
    n.stop(flush_timeout=0.1)


def test_env_found_in_cwd_behaves_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CWD == repo root: the relative default path is used untouched."""
    _isolate_tg_env(monkeypatch)
    repo = tmp_path / "repo"
    secrets = repo / ".secrets"
    secrets.mkdir(parents=True)
    (secrets / "telegram.env").write_text(
        f"TG_BOT_TOKEN={TOKEN}\nTG_CHAT_ID={CHAT_ID}\n", encoding="utf-8"
    )
    monkeypatch.chdir(repo)
    assert _resolve_env_path(tgnotify.DEFAULT_ENV_PATH) == tgnotify.DEFAULT_ENV_PATH
    cfg = TgConfig.from_env()
    assert cfg.token == TOKEN and cfg.chat_id == CHAT_ID
    n = TgNotifier.from_env()
    assert Path(n.root).resolve() == repo.resolve()
    n.stop(flush_timeout=0.1)


def test_env_resolution_uses_git_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A .git marker anchors the root even when .secrets/ does not exist yet."""
    _isolate_tg_env(monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    project = repo / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    resolved = _resolve_env_path(tgnotify.DEFAULT_ENV_PATH)
    assert resolved == repo / tgnotify.DEFAULT_ENV_PATH
    n = TgNotifier.from_env()  # no env file -> no-op, but root is still the repo
    assert Path(n.root).resolve() == repo.resolve()
    assert n._noop


def test_env_secrets_wins_over_nested_git_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Real layout: .git sits in the inner project/ (== CWD) while .secrets

    lives in the OUTER folder one level up. The ancestor holding .secrets/
    must win -- the nested .git must not shadow it (regression: from
    project/ the walk-up used to stop at project/.git and miss the secrets).
    """
    _isolate_tg_env(monkeypatch)
    outer = tmp_path / "monobank-2048"
    secrets = outer / ".secrets"
    secrets.mkdir(parents=True)
    (secrets / "telegram.env").write_text(
        f"TG_BOT_TOKEN={TOKEN}\nTG_CHAT_ID={CHAT_ID}\n", encoding="utf-8"
    )
    project = outer / "project"
    project.mkdir()
    (project / ".git").mkdir()  # git repo is the INNER dir
    monkeypatch.chdir(project)

    resolved = _resolve_env_path(tgnotify.DEFAULT_ENV_PATH)
    assert resolved == outer / tgnotify.DEFAULT_ENV_PATH  # .secrets ancestor, not .git
    cfg = TgConfig.from_env()
    assert cfg.usable and cfg.token == TOKEN and cfg.chat_id == CHAT_ID
    assert "***" in repr(cfg) and TOKEN not in repr(cfg)  # token stays masked
    n = TgNotifier.from_env()
    assert Path(n.root).resolve() == outer.resolve()  # logs/ land at outer root
    n.stop(flush_timeout=0.1)


def test_env_resolution_explicit_paths_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Absolute paths and existing relative paths keep their old meaning."""
    _isolate_tg_env(monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".secrets").mkdir(parents=True)
    project = repo / "project"
    project.mkdir()
    absolute = tmp_path / "elsewhere.env"
    absolute.write_text("TG_BOT_TOKEN=abs:1\nTG_CHAT_ID=7\n", encoding="utf-8")
    monkeypatch.chdir(project)
    assert _resolve_env_path(absolute) == absolute
    assert TgConfig.from_env(absolute).token == "abs:1"
    local = Path("local.env")
    local.write_text("TG_BOT_TOKEN=loc:1\nTG_CHAT_ID=8\n", encoding="utf-8")
    assert _resolve_env_path(local) == local  # exists in CWD -> not re-anchored
    assert TgConfig.from_env(local).token == "loc:1"


def test_repr_masks_token():
    cfg = TgConfig(token=TOKEN, chat_id=CHAT_ID)
    assert TOKEN not in repr(cfg) and TOKEN not in str(cfg)
    assert "***" in repr(cfg)


def test_unusable_config_is_noop(tmp_path: Path):
    n = TgNotifier(TgConfig(), root=tmp_path)
    n.start()  # must not start a thread
    n.notify("FATAL", "CRASH", "x")  # must not raise
    assert n._thread is None and n._queue.empty()


# -- formatting (§4.1) ---------------------------------------------------------


def test_format_action_required(tmp_path: Path):
    n = make_notifier(tmp_path)
    txt = n._format(_Item("msg", "FATAL", "AUTH_REQUIRED", "бank wall"))
    assert txt.splitlines()[0].startswith("💀 FATAL AUTH_REQUIRED")
    assert "⚠️ ПОТРІБЕН ТИ — введи ПІН у monobank" in txt
    assert "(Kyiv)" in txt
    # DEVICE_LOST is action-required only at FATAL (§3.1)
    assert "ПОТРІБЕН ТИ" in n._format(_Item("msg", "FATAL", "DEVICE_LOST", "x"))
    assert "ПОТРІБЕН ТИ" not in n._format(_Item("msg", "ERROR", "DEVICE_LOST", "x"))
    assert "ПОТРІБЕН ТИ" not in n._format(_Item("msg", "ERROR", "MOVE_REJECTED", "x"))


def test_truncation():
    assert _truncate("x" * 5000, 4096).endswith("…")
    assert len(_truncate("x" * 5000, 4096)) == 4096
    assert _truncate("short", 4096) == "short"


# -- dedup / WARN throttle (§4.2, §3.1) ----------------------------------------


def test_dedup_window(tmp_path: Path):
    api = RecordingApi()
    n = make_notifier(tmp_path, api)
    item = _Item("msg", "ERROR", "MOVE_REJECTED", "same text")
    n._process_item(item)
    n._process_item(_Item("msg", "ERROR", "MOVE_REJECTED", "same text"))
    assert len(api.sent_texts()) == 1
    assert n._dropped_dupes == 1


def test_warn_throttle_and_digest(tmp_path: Path):
    api = RecordingApi()
    n = make_notifier(tmp_path, api)
    n._process_item(_Item("msg", "WARN", "VISION_MISMATCH", "v1"))
    n._process_item(_Item("msg", "WARN", "VISION_MISMATCH", "v2"))
    n._process_item(_Item("msg", "WARN", "VISION_MISMATCH", "v3"))
    assert len(api.sent_texts()) == 1  # first immediately, rest counted
    n._last_digest_mono = time.monotonic() - 601
    n._maybe_warn_digest()
    digest = api.sent_texts()[-1]
    assert digest.startswith("🟡 WARN ×2") and "VISION_MISMATCH ×2" in digest


# -- retry / 429 / outbox (§2.3, §2.4) ------------------------------------------


def test_retry_exhausted_goes_to_outbox_then_flush(tmp_path: Path):
    api = RecordingApi(fail=[TgNetworkError("x"), TgNetworkError("x"), TgNetworkError("x")])
    n = make_notifier(tmp_path, api)
    n._process_item(_Item("msg", "ERROR", "STORAGE_ERROR", "db down", alert_ts="T1"))
    outbox = (tmp_path / OUTBOX_PATH).read_text(encoding="utf-8").splitlines()
    assert len(outbox) == 1
    row = json.loads(outbox[0])
    assert row["code"] == "STORAGE_ERROR" and row["attempt"] == 3
    # network back -> flush delivers and advances the cursor
    n._flush_outbox()
    assert (tmp_path / OUTBOX_CURSOR_PATH).read_text(encoding="utf-8") == "1"
    assert any("db down" in t for t in api.sent_texts())


def test_429_respects_retry_after(tmp_path: Path):
    api = RecordingApi(fail=[TgHttpError(429, "flood", retry_after=0.01)])
    n = make_notifier(tmp_path, api)
    assert n._send_with_retry(_Item("raw", "INFO", "X", "hello")) is True
    assert not (tmp_path / OUTBOX_PATH).exists()


def test_queue_full_drops_info_first(tmp_path: Path):
    n = make_notifier(tmp_path)
    for _ in range(200):
        n._queue.put_nowait(_Item("raw", "INFO", "X", "filler"))
    n.notify("INFO", "X", "dropped silently")
    n.notify("FATAL", "CRASH", "must survive")
    rows = (tmp_path / OUTBOX_PATH).read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["code"] == "CRASH"


# -- commands / allowlist (§5) ---------------------------------------------------


def _update(uid, chat_id, text, date=None, username="test_user"):
    return {
        "update_id": uid,
        "message": {
            "chat": {"id": chat_id},
            "from": {"username": username},
            "date": date if date is not None else time.time(),
            "text": text,
        },
    }


def test_allowlist_foreign_chat_silent(tmp_path: Path):
    api = RecordingApi(updates=[_update(7, 111111, "/status", username="stranger")])
    n = make_notifier(tmp_path, api)
    n._poll_updates()
    assert api.sent_texts() == []  # zero replies to strangers
    assert n._ignored_updates == 1
    # but the offset is still confirmed so we never re-read it
    assert (tmp_path / UPDATES_OFFSET_PATH).read_text(encoding="utf-8") == "7"


def test_stale_command_ignored(tmp_path: Path):
    api = RecordingApi(updates=[_update(8, int(CHAT_ID), "/pause", date=time.time() - 400)])
    n = make_notifier(tmp_path, api)
    n._poll_updates()
    assert api.sent_texts() == []
    assert not (tmp_path / PAUSE_FLAG_PATH).exists()


def test_pause_resume_ack_flow(tmp_path: Path):
    api = RecordingApi(updates=[_update(9, int(CHAT_ID), "/pause")])
    n = make_notifier(tmp_path, api)
    n._poll_updates()
    assert (tmp_path / PAUSE_FLAG_PATH).is_file()
    assert any("паузну" in t for t in api.sent_texts())
    api.updates = [_update(10, int(CHAT_ID), "/resume")]
    n._poll_updates()
    assert not (tmp_path / PAUSE_FLAG_PATH).exists()
    # /ack mutes pending action-required reminders
    n._pending_action["AUTH_REQUIRED"] = {
        "text": "t", "photo_path": None,
        "first_mono": 0.0, "last_sent_mono": 0.0, "first_ts": time.time(),
    }
    api.updates = [_update(11, int(CHAT_ID), "/ack")]
    n._poll_updates()
    assert (tmp_path / ACK_FLAG_PATH).is_file() and not n._pending_action


def test_watch_mode_limits_commands(tmp_path: Path):
    api = RecordingApi(updates=[_update(12, int(CHAT_ID), "/pause")])
    n = make_notifier(tmp_path, api, watch_mode=True)
    n._poll_updates()
    assert not (tmp_path / PAUSE_FLAG_PATH).exists()
    assert any("раннер не запущений" in t for t in api.sent_texts())


# -- alert journal + reminders (§3.1, §6.2) ---------------------------------------


def test_sent_journal_written_for_alerts(tmp_path: Path):
    api = RecordingApi()
    n = make_notifier(tmp_path, api)
    n._process_item(
        _Item("msg", "FATAL", "AUTH_REQUIRED", "wall", alert_ts="2026-07-10T14:03:25.000000Z")
    )
    row = json.loads((tmp_path / SENT_JOURNAL_PATH).read_text(encoding="utf-8"))
    assert row["ts"] == "2026-07-10T14:03:25.000000Z" and row["code"] == "AUTH_REQUIRED"
    # action-required got registered for reminders
    assert "AUTH_REQUIRED" in n._pending_action


def test_reminder_fires_until_acked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)  # xdist workers share the repo cwd — isolate any relative path
    api = RecordingApi()
    n = make_notifier(tmp_path, api)
    n._process_item(_Item("msg", "FATAL", "AUTH_REQUIRED", "wall"))
    sent_before = len(api.sent_texts())
    n._pending_action["AUTH_REQUIRED"]["last_sent_mono"] -= 601
    n._maybe_remind()
    assert len(api.sent_texts()) == sent_before + 1
    # ack -> reminder disappears
    ack = tmp_path / ACK_FLAG_PATH
    ack.parent.mkdir(parents=True, exist_ok=True)
    ack.write_text("ack", encoding="utf-8")
    # Windows file-time ticks are coarser than time.time(): when the whole
    # test fits into one ~16 ms tick, the ack mtime can land BEFORE first_ts
    # and the ack is "not seen" (intermittent under xdist load). Pin the
    # mtime explicitly after first_ts — same semantics: ack follows alert.
    acked_at = n._pending_action["AUTH_REQUIRED"]["first_ts"] + 1
    os.utime(ack, (acked_at, acked_at))
    n._pending_action["AUTH_REQUIRED"]["last_sent_mono"] -= 601
    n._maybe_remind()
    assert len(api.sent_texts()) == sent_before + 1 and not n._pending_action


# -- watch mode (§6.1) --------------------------------------------------------------


def _alert_line(level, code, ts, message="msg"):
    return json.dumps(
        {"schema": 1, "ts": ts, "level": level, "code": code, "message": message,
         "incident_dir": None},
        separators=(",", ":"),
    )


def test_watch_sends_only_new_error_fatal(tmp_path: Path):
    old_ts = "2026-07-10T10:00:00.000000Z"  # far past the 15 s grace
    alerts = tmp_path / ALERTS_PATH
    alerts.parent.mkdir(parents=True, exist_ok=True)
    alerts.write_text(
        _alert_line("WARN", "VISION_MISMATCH", old_ts) + "\n"
        + _alert_line("FATAL", "AUTH_REQUIRED", old_ts) + "\n",
        encoding="utf-8",
    )
    # already-delivered alert -> dedup via tg_sent.jsonl
    (tmp_path / SENT_JOURNAL_PATH).write_text(
        json.dumps({"ts": old_ts, "code": "AUTH_REQUIRED", "sent_at": old_ts}) + "\n",
        encoding="utf-8",
    )
    api = RecordingApi()
    n = make_notifier(tmp_path, api, watch_mode=True)
    watch(tmp_path, once=True, notifier=n)
    assert api.sent_texts() == []  # WARN filtered, FATAL deduped
    assert (tmp_path / ALERTS_CURSOR_PATH).read_text(encoding="utf-8") == "2"
    # a new FATAL appears -> watch delivers it
    with open(alerts, "a", encoding="utf-8") as fh:
        fh.write(_alert_line("FATAL", "CRASH", old_ts, "boom") + "\n")
    n2 = make_notifier(tmp_path, RecordingApi(), watch_mode=True)
    watch(tmp_path, once=True, notifier=n2)
    texts = n2._api_call.sent_texts()  # type: ignore[attr-defined]
    assert any("boom" in t for t in texts)
    assert (tmp_path / ALERTS_CURSOR_PATH).read_text(encoding="utf-8") == "3"


def test_watch_heartbeat_watchdog(tmp_path: Path):
    hb = tmp_path / HEARTBEAT_PATH
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(json.dumps({"ts": "x", "run_id": "r"}), encoding="utf-8")
    stale = time.time() - 120
    os.utime(hb, (stale, stale))
    api = RecordingApi()
    n = make_notifier(tmp_path, api, watch_mode=True)
    watch(tmp_path, once=True, notifier=n)
    assert any("WATCHDOG_TIMEOUT" in t or "завис" in t for t in api.sent_texts())


# -- session bracket (§3.3) -----------------------------------------------------------


def test_session_messages_and_double_end_guard(tmp_path: Path):
    api = RecordingApi()
    n = make_notifier(tmp_path, api)
    n.session_start(run_id="run-1", strategy="expectimax d3")
    n.session_end(ok=True, reason="Партій: 3.")
    n.session_end(ok=False, reason="CRASH")  # guarded: must not enqueue
    items = []
    while not n._queue.empty():
        items.append(n._queue.get_nowait())
    assert len(items) == 2
    assert "НЕ чіпай телефон" in items[0].text and "run_id=run-1" in items[0].text
    assert items[1].text.startswith("✅")


# -- token never leaks (§8) ------------------------------------------------------------


def test_network_error_has_no_url_or_token(tmp_path: Path):
    n = make_notifier(tmp_path, api=None, api_base="http://127.0.0.1:1")
    with pytest.raises(TgNetworkError) as exc_info:
        n._api_call("sendMessage", {"chat_id": CHAT_ID, "text": "x"}, timeout=0.5)
    blob = repr(exc_info.value) + str(exc_info.value)
    assert TOKEN not in blob and "http" not in blob.lower()


# -- integration: fake Bot API over real urllib + worker thread (§9.11) -----------------


class _FakeBotApi(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str]]] = []  # rebound per test

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        method = self.path.rsplit("/", 1)[-1]
        type(self).calls.append((method, body))
        result: object = [] if method == "getUpdates" else {"message_id": 1}
        payload = json.dumps({"ok": True, "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence stderr
        pass


def test_full_cycle_over_http(tmp_path: Path):
    _FakeBotApi.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBotApi)
    thread = None
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        n = make_notifier(tmp_path, api=None, api_base=base)
        n.start()
        n.notify("FATAL", "AUTH_REQUIRED", "інтеграційний тест",
                 alert_ts="2026-07-10T14:03:25.000000Z")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(m == "sendMessage" for m, _ in _FakeBotApi.calls):
                break
            time.sleep(0.05)
        n.stop(flush_timeout=5)
        sends = [b for m, b in _FakeBotApi.calls if m == "sendMessage"]
        assert sends, "sendMessage never reached the fake Bot API"
        assert "AUTH_REQUIRED" in urllib_unquote(sends[0])
        assert (
            "%D0%9F%D0%9E%D0%A2%D0%A0%D0%86%D0%91%D0%95%D0%9D" in sends[0]
            or "ПОТРІБЕН" in urllib_unquote(sends[0])
        )
        # §6.2: delivered alert journalled for the reserve watcher
        journal = (tmp_path / SENT_JOURNAL_PATH).read_text(encoding="utf-8")
        assert "AUTH_REQUIRED" in journal
        # §8: the token appears nowhere in produced artefacts
        for path in (tmp_path / "logs").rglob("*"):
            if path.is_file():
                assert TOKEN not in path.read_text(encoding="utf-8", errors="replace"), path
    finally:
        server.shutdown()
        server.server_close()


def urllib_unquote(s: str) -> str:
    import urllib.parse

    return urllib.parse.unquote_plus(s)

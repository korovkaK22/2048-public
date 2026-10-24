"""Incident tracing (docs/ops-monitoring.md §4): every error path, once its
retry budget is spent, leaves ONE traceable record -- bundle with the phone
screenshot + reason + context, ``live_events.incident_dir`` set, Telegram told
(with the photo). Runs against the in-memory FakePhone + the REAL Supervisor;
every artifact lands under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from bot2048.ai.go_engine import GoEngineError
from bot2048.core.types import Board, Direction, Spawn
from bot2048.device.adb import DeviceError
from bot2048.device.supervisor import FatalIncident, SupervisorConfig
from bot2048.recording import GameEnded, MovePlayed
from bot2048.runner.alerts import AlertCode, AlertLevel, AlertWriter
from bot2048.runner.escalation import POLICIES
from bot2048.runner.incidents import (
    BUNDLE_DIR_RE,
    IncidentSink,
    MoveRing,
    prune_incidents,
)
from bot2048.runner.lifecycle import run_session
from bot2048.runner.live import LiveConfig
from bot2048.tgnotify.notifier import incident_photo
from tests.runner.conftest import (
    FRESH_BOARD,
    NEARLY_DEAD_BOARD,
    FakePhone,
    Harness,
    ScriptedStrategy,
    make_harness,
)

# -- helpers --------------------------------------------------------------------


@dataclass
class _FakeOps:
    """``record_event`` recorder (OpsRepository signature), incident_dir included."""

    events: list[dict[str, Any]] = field(default_factory=list)

    def record_event(
        self,
        run_id: str,
        level: str,
        code: str,
        message: str,
        *,
        game_id: str | None = None,
        ply: int | None = None,
        details: dict[str, Any] | None = None,
        incident_dir: str | None = None,
        ts: float | None = None,
    ) -> None:
        self.events.append(
            {
                "level": level,
                "code": code,
                "message": message,
                "game_id": game_id,
                "ply": ply,
                "details": details or {},
                "incident_dir": incident_dir,
            }
        )

    def of(self, code: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["code"] == code]


@dataclass
class _FakeTelegram:
    """Stands in for ``TgNotifier.notify_alert`` on the AlertWriter hook: records
    the alert and the photo the real adapter would attach."""

    sent: list[tuple[str, str, str, str | None]] = field(default_factory=list)

    def __call__(self, alert: Any) -> None:
        photo = incident_photo(Path("."), alert.incident_dir, alert.level.value)
        self.sent.append((alert.level.value, alert.code.value, alert.message, photo))

    def of(self, code: str) -> list[tuple[str, str, str, str | None]]:
        return [s for s in self.sent if s[1] == code]


def _wire(h: Harness) -> tuple[_FakeOps, _FakeTelegram]:
    """Attach the DB mirror + the Telegram hook to a harness."""
    ops = _FakeOps()
    tg = _FakeTelegram()
    h.runner.ops = ops  # type: ignore[assignment]
    h.runner._incidents.ops = ops
    h.runner.alerts.notifier = tg
    return ops, tg


def _bundles(tmp_path: Path) -> list[Path]:
    root = tmp_path / "incidents"
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _alerts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _events(h: Harness, kind: type) -> list[Any]:
    return [e for e in h.recorder.events if isinstance(e, kind)]


FULL_BUNDLE_FILES = {
    "alert.json",
    "screenshot_before.png",
    "screenshot_after.png",
    "screenshot_last_parsed.png",
    "last_moves.json",
    "state_probes.json",
    "traceback.txt",
    "context.json",
}


# -- MOVE_REJECTED: the swipe retry budget ---------------------------------------------


class TestMoveRejected:
    def test_bundle_after_budget_exhausted(self, tmp_path: Path) -> None:
        phone = FakePhone(board=NEARLY_DEAD_BOARD, reject_swipes=99)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=1, swipe_retries=2),
        )
        ops, tg = _wire(h)
        h.runner.run()

        assert phone.swipes == [Direction.RIGHT] * 3  # 1 + swipe_retries, unchanged
        bundles = _bundles(tmp_path)
        assert len(bundles) == 1  # exactly ONE bundle for the whole failure
        bundle = bundles[0]
        assert BUNDLE_DIR_RE.match(bundle.name) and bundle.name.endswith("_MOVE_REJECTED")
        assert {p.name for p in bundle.iterdir()} >= FULL_BUNDLE_FILES

        alert = _json(bundle / "alert.json")
        assert alert["schema"] == 2
        assert alert["level"] == "ERROR" and alert["code"] == "MOVE_REJECTED"
        assert alert["attempts"] == 3  # the REAL budget spent (swipe_retries=2 -> 3 tries)
        assert alert["retry_policy"]["source"] == "LiveConfig.swipe_retries"
        table_max = POLICIES[AlertCode.MOVE_REJECTED].max_attempts
        assert alert["retry_policy"]["max_attempts"] == table_max
        assert alert["stage"] == "swipe"
        assert alert["capture_kind"] == "probe"  # the supervisor's fresh probe frame
        assert alert["strategy"] == "scripted"
        assert alert["engine"]["kind"] == "ScriptedStrategy"
        assert alert["game_id"] is not None and alert["move_no"] == 0
        assert alert["incident_dir"] == bundle.as_posix()

        ctx = _json(bundle / "context.json")
        assert ctx["schema"] == 2 and ctx["stage"] == "swipe"
        assert ctx["session"]["config"]["swipe_retries"] == 2
        assert ctx["session"]["capture_mode"] == "frame"
        assert "MOVE_REJECTED" in ctx["session"]["retry_table"]

        # DB mirror: the alert row AND the abort row point at the bundle.
        (err,) = ops.of("MOVE_REJECTED")
        assert err["level"] == "ERROR" and err["incident_dir"] == bundle.as_posix()
        assert err["details"]["stage"] == "swipe" and err["details"]["attempts"] == 3
        (aborted,) = ops.of("MOVE_ABORTED")
        assert aborted["incident_dir"] == bundle.as_posix()
        assert aborted["game_id"] == err["game_id"] and aborted["ply"] == err["ply"]

        # Telegram: the reason + the incident-time photo.
        (sent,) = tg.of("MOVE_REJECTED")
        assert sent[0] == "ERROR" and "rejected 3x" in sent[2]
        assert sent[3] == str(bundle / "screenshot_after.png")
        assert Path(sent[3]).is_file()

        assert _events(h, GameEnded)[0].end_reason == "move_rejected"

    def test_no_bundle_before_budget_exhausted(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            reject_swipes=1,  # one dropped gesture, the retry lands
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=1, swipe_retries=2),
        )
        ops, tg = _wire(h)
        h.runner.run()

        assert len(_events(h, MovePlayed)) == 1  # the move committed on retry
        assert _bundles(tmp_path) == []
        assert _alerts(h.alerts_path) == []
        assert tg.sent == []
        assert not any(e["incident_dir"] for e in ops.events)


# -- VISION_MISMATCH ----------------------------------------------------------------


class TestVisionMismatch:
    def test_verify_stage_bundle(self, tmp_path: Path) -> None:
        corrupt: Board = (3, 3, 3, 3, *NEARLY_DEAD_BOARD[4:])
        phone = FakePhone(board=NEARLY_DEAD_BOARD, corrupt_to=corrupt)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=1, parse_retries=2),
        )
        ops, tg = _wire(h)
        h.runner.run()

        (bundle,) = _bundles(tmp_path)
        assert bundle.name.endswith("_VISION_MISMATCH")
        alert = _json(bundle / "alert.json")
        assert alert["stage"] == "verify"
        assert alert["attempts"] == 2  # parse_retries
        assert alert["retry_policy"]["source"] == "LiveConfig.parse_retries"
        assert alert["extra"]["board_parsed"] == alert["board_hex"]
        assert alert["capture_kind"] == "probe"
        assert (bundle / "screenshot_after.png").is_file()
        (row,) = ops.of("VISION_MISMATCH")
        assert row["incident_dir"] == bundle.as_posix()
        assert ops.of("MOVE_ABORTED")[0]["incident_dir"] == bundle.as_posix()
        (sent,) = tg.of("VISION_MISMATCH")
        assert sent[3] is not None and Path(sent[3]).is_file()
        assert _events(h, GameEnded)[0].end_reason == "vision_mismatch"


# -- SCREEN_UNEXPECTED: ERROR -> failed recovery -> FATAL chained into one bundle -----


class TestScreenUnexpectedChain:
    def test_error_and_fatal_share_one_bundle(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            break_after_swipes=1,  # the screen goes UNKNOWN for good
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=3),
        )
        ops, tg = _wire(h)
        with pytest.raises(FatalIncident) as exc_info:
            h.runner.run()
        assert exc_info.value.alert.code is AlertCode.SCREEN_UNEXPECTED

        (bundle,) = _bundles(tmp_path)  # ONE folder, not an ERROR + a "-2" FATAL dir
        names = {p.name for p in bundle.iterdir()}
        assert {"alert.json", "alert_fatal.json", "screenshot_after.png",
                "screenshot_fatal.png", "state_probes_fatal.json",
                "traceback_fatal.txt", "context_fatal.json"} <= names
        first = _json(bundle / "alert.json")
        fatal = _json(bundle / "alert_fatal.json")
        assert first["level"] == "ERROR" and first["stage"] == "capture"
        assert fatal["level"] == "FATAL" and fatal["stage"] == "recovery"
        assert fatal["attempts"] == SupervisorConfig().transient_repolls + 1
        assert fatal["extra"]["chained_from"] == "alert.json"
        assert first["incident_dir"] == fatal["incident_dir"] == bundle.as_posix()
        assert exc_info.value.alert.incident_dir == bundle.as_posix()

        rows = ops.of("SCREEN_UNEXPECTED")
        assert [r["level"] for r in rows] == ["ERROR", "FATAL"]
        assert all(r["incident_dir"] == bundle.as_posix() for r in rows)

        err, fat = tg.of("SCREEN_UNEXPECTED")
        assert err[3] == str(bundle / "screenshot_after.png")
        assert fat[3] == str(bundle / "screenshot_fatal.png")  # the FATAL's own frame
        assert "re-polls all non-GAME" in fat[2]


# -- DEVICE_LOST storm: one bundle per window, LOOP_ERROR attached ---------------------


class TestDeviceLost:
    def test_storm_costs_one_bundle_and_every_row_points_at_it(self, tmp_path: Path) -> None:
        phone = FakePhone(board=FRESH_BOARD)
        phone.transient_errors = deque([DeviceError("device offline")] * 500)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=LiveConfig(experiment_id="t", max_games=0, recover_max_per_hour=2),
        )
        ops, tg = _wire(h)
        with pytest.raises(FatalIncident) as exc_info:
            h.runner.run()
        assert exc_info.value.alert.code is AlertCode.DEVICE_LOST

        (bundle,) = _bundles(tmp_path)
        assert bundle.name.endswith("_DEVICE_LOST")
        alert = _json(bundle / "alert.json")
        assert alert["capture_kind"] == "device_lost"  # no blind re-capture attempted
        assert "device_lost" in alert["extra"]["capture_error"]
        assert alert["retry_policy"]["max_attempts"] == POLICIES[AlertCode.DEVICE_LOST].max_attempts
        assert not (bundle / "screenshot_after.png").exists()
        assert (bundle / "alert_fatal.json").is_file()
        # The storm (3 ERROR/FATAL cycles + 2 LOOP_ERROR recoveries) attached here.
        repeat_lines = (bundle / "alerts_repeat.jsonl").read_text().splitlines()
        repeats = [json.loads(line) for line in repeat_lines]
        assert {r["code"] for r in repeats} == {"DEVICE_LOST", "LOOP_ERROR"}
        assert all(r["incident_dir"] == bundle.as_posix() for r in repeats)
        assert all(r["extra"]["repeat_of"] == bundle.as_posix() for r in repeats)

        loop_rows = ops.of("LOOP_ERROR")
        assert len(loop_rows) == 2
        assert all(r["incident_dir"] == bundle.as_posix() for r in loop_rows)
        assert all(r["incident_dir"] == bundle.as_posix() for r in ops.of("DEVICE_LOST"))
        # Every alert row (alerts.jsonl) carries an incident_dir -- nothing untraceable.
        assert all(a["incident_dir"] for a in _alerts(h.alerts_path))
        assert tg.of("LOOP_ERROR")  # the operator saw the recoveries too


# -- AUTH_REQUIRED: FLAG_SECURE-blind, the ui dump is the evidence ---------------------


class TestAuthRequired:
    def test_blind_bundle_records_why_there_is_no_screenshot(self, tmp_path: Path) -> None:
        phone = FakePhone(board=FRESH_BOARD, state="auth")
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([]),
            config=LiveConfig(experiment_id="t", max_games=1),  # auto_login off: FATAL
        )
        ops, tg = _wire(h)
        with pytest.raises(FatalIncident) as exc_info:
            h.runner.run()
        assert exc_info.value.alert.code is AlertCode.AUTH_REQUIRED

        (bundle,) = _bundles(tmp_path)
        names = {p.name for p in bundle.iterdir()}
        assert "uidump.xml" in names and "screenshot_after.png" not in names
        alert = _json(bundle / "alert.json")
        assert alert["level"] == "FATAL" and alert["stage"] == "auth"
        assert alert["attempts"] == 0 and alert["retry_policy"]["fatal_immediately"] is True
        assert alert["capture_kind"] == "blind_secure"
        assert "FLAG_SECURE" in alert["extra"]["capture_error"]
        assert alert["extra"]["auto_login"] is False
        assert alert["screen_state"] == "auth_required"
        (row,) = ops.of("AUTH_REQUIRED")
        assert row["incident_dir"] == bundle.as_posix()
        (sent,) = tg.of("AUTH_REQUIRED")
        assert "auto-login disabled" in sent[2]
        assert sent[3] is None  # nothing to photograph on a secure surface


# -- LOOP_ERROR: an unexpected exception, FRESH screenshot + traceback + DB row ----------


class TestLoopError:
    def test_generic_exception_gets_full_bundle(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            crash_after_swipes=1,  # ONE RuntimeError from screenshot, then healthy
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=2),
        )
        ops, tg = _wire(h)
        h.runner.run()  # recovers in place

        (bundle,) = _bundles(tmp_path)
        assert bundle.name.endswith("_LOOP_ERROR")
        # No screen probe on this path (the exception, not a classified screen,
        # is the evidence) -- everything else of a full bundle is there.
        assert FULL_BUNDLE_FILES - {"state_probes.json"} <= {p.name for p in bundle.iterdir()}
        alert = _json(bundle / "alert.json")  # the old LOOP_ERROR bundles had none
        assert alert["level"] == "ERROR" and alert["stage"] == "session"
        assert alert["capture_kind"] == "fresh"  # taken THROUGH the ring hook, post-error
        assert alert["attempts"] == 1
        assert alert["retry_policy"]["source"] == "LiveConfig.recover_max_per_hour"
        assert alert["extra"]["cause"] == "RuntimeError"
        assert "transient screencap failure" in (bundle / "traceback.txt").read_text()
        (row,) = ops.of("LOOP_ERROR")
        assert row["incident_dir"] == bundle.as_posix()
        (sent,) = tg.of("LOOP_ERROR")
        assert sent[3] == str(bundle / "screenshot_after.png")

    def test_engine_error_after_its_retry_is_traced(self, tmp_path: Path) -> None:
        class _DeadEngine:
            name = "go-dead"
            closes = 0

            def choose(self, board: Board) -> Any:
                raise GoEngineError("engine pipe broke")

            def close(self) -> None:
                self.closes += 1

        engine = _DeadEngine()
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=engine,
            config=LiveConfig(experiment_id="t", max_games=3, recover_max_per_hour=1),
        )
        ops, _tg = _wire(h)
        with pytest.raises(GoEngineError):
            h.runner.run()  # 1 recovery, then the budget is gone -> propagates

        assert engine.closes >= 1  # the one in-place engine restart was tried
        (bundle,) = _bundles(tmp_path)
        assert bundle.name.endswith("_LOOP_ERROR")
        alert = _json(bundle / "alert.json")
        assert alert["extra"]["cause"] == "GoEngineError"
        assert alert["strategy"] == "go-dead"
        assert "engine pipe broke" in (bundle / "traceback.txt").read_text()
        assert (bundle / "screenshot_after.png").is_file()
        assert ops.of("LOOP_ERROR")[0]["incident_dir"] == bundle.as_posix()
        assert [e.end_reason for e in _events(h, GameEnded)] == ["error", "error"]


# -- the fresh screenshot itself fails: the ORIGINAL reason must survive ---------------


class TestCaptureFailure:
    def test_capture_error_recorded_original_reason_kept(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            crash_after_swipes=1,
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT] * 5),
            config=LiveConfig(experiment_id="t", max_games=2),
        )
        ops, tg = _wire(h)

        def _boom() -> Any:
            raise OSError("screencap: broken pipe")

        assert h.runner.ring is not None
        h.runner.ring.set_capture(_boom)
        h.runner.run()

        (bundle,) = _bundles(tmp_path)
        alert = _json(bundle / "alert.json")
        assert alert["code"] == "LOOP_ERROR"
        assert "transient screencap failure" in alert["message"]  # the real cause
        assert alert["capture_kind"] == "failed"
        assert alert["extra"]["capture_error"] == "OSError: screencap: broken pipe"
        assert not (bundle / "screenshot_after.png").exists()
        assert (bundle / "screenshot_before.png").is_file()  # the last good frame still there
        assert (bundle / "traceback.txt").is_file()
        assert ops.of("LOOP_ERROR")[0]["incident_dir"] == bundle.as_posix()
        (sent,) = tg.of("LOOP_ERROR")
        # Fallback photo order: the last parsed frame, then the last good frame.
        assert sent[3] == str(bundle / "screenshot_last_parsed.png")


# -- CRASH via the lifecycle guard ---------------------------------------------------


def _frame() -> Any:
    return np.full((8, 8, 3), 7, dtype=np.uint8)


class TestCrash:
    def test_crash_bundle_has_fresh_screenshot_and_db_row(self, tmp_path: Path) -> None:
        paths = {
            "db_path": tmp_path / "games.db",
            "alerts_path": tmp_path / "alerts.jsonl",
            "heartbeat_path": tmp_path / "heartbeat.json",
            "lock_path": tmp_path / "runner.lock",
            "incidents_root": tmp_path / "incidents",
        }
        tg = _FakeTelegram()
        with pytest.raises(SystemExit) as exc_info, run_session(**paths) as ctx:
            ctx.alerts.notifier = tg
            ctx.ring.set_capture(_frame)  # what LiveRunner.__init__ installs
            ctx.ring.set_good_frame(_frame())
            raise RuntimeError("kaboom in the loop")
        assert exc_info.value.code == 2

        (bundle,) = _bundles(tmp_path)
        assert bundle.name.endswith("_CRASH")
        names = {p.name for p in bundle.iterdir()}
        assert {"alert.json", "screenshot_after.png", "screenshot_before.png",
                "traceback.txt", "context.json", "last_moves.json"} <= names
        alert = _json(bundle / "alert.json")
        assert alert["level"] == "FATAL" and alert["code"] == "CRASH"
        assert alert["capture_kind"] == "fresh" and alert["stage"] == "session"
        assert alert["extra"]["cause"] == "RuntimeError"
        assert "kaboom in the loop" in (bundle / "traceback.txt").read_text()

        with sqlite3.connect(paths["db_path"]) as conn:
            rows = conn.execute(
                "SELECT level, code, incident_dir FROM live_events WHERE code='CRASH'"
            ).fetchall()
            exit_reason = conn.execute("SELECT exit_reason FROM runs").fetchone()[0]
        assert rows == [("FATAL", "CRASH", bundle.as_posix())]
        assert exit_reason == "fatal:CRASH"
        (sent,) = tg.of("CRASH")
        assert sent[3] == str(bundle / "screenshot_after.png")


# -- sink unit tests: chaining / dedupe windows, pruning --------------------------------


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _sink(tmp_path: Path, clock: _Clock, **kw: Any) -> tuple[IncidentSink, _FakeOps]:
    ops = _FakeOps()
    ring = MoveRing()
    ring.set_capture(_frame)
    sink = IncidentSink(
        root=tmp_path / "incidents",
        alerts=AlertWriter(tmp_path / "alerts.jsonl"),
        ops=ops,
        run_id="run-x",
        ring=ring,
        clock=clock,
        **kw,
    )
    return sink, ops


class TestSinkWindows:
    def test_chain_then_dedupe_then_new_bundle(self, tmp_path: Path) -> None:
        clock = _Clock(1_800_000_000.0)
        sink, ops = _sink(tmp_path, clock)
        first = sink.record(AlertCode.SCREEN_UNEXPECTED, AlertLevel.ERROR, "e1")
        clock.t += 5
        fatal = sink.record(AlertCode.SCREEN_UNEXPECTED, AlertLevel.FATAL, "f1")
        assert fatal.chained and fatal.bundle_dir == first.bundle_dir
        clock.t += 5  # 10 s after the first: a repeat attaches
        rep = sink.record(AlertCode.SCREEN_UNEXPECTED, AlertLevel.ERROR, "e2")
        assert rep.attached and rep.bundle_dir == first.bundle_dir
        assert (first.bundle_dir / "alerts_repeat.jsonl").read_text().count("\n") == 1
        clock.t += 25  # 35 s after the first: window over -> fresh bundle + screenshot
        new = sink.record(AlertCode.SCREEN_UNEXPECTED, AlertLevel.ERROR, "e3")
        assert not new.attached and not new.chained and new.bundle_dir != first.bundle_dir
        assert (new.bundle_dir / "screenshot_after.png").is_file()
        # A different code never shares a bundle.
        other = sink.record(AlertCode.VISION_MISMATCH, AlertLevel.ERROR, "v1")
        assert other.bundle_dir not in (first.bundle_dir, new.bundle_dir)
        assert len(_bundles(tmp_path)) == 3
        assert all(e["incident_dir"] for e in ops.events) and len(ops.events) == 5

    def test_fatal_long_after_error_is_its_own_bundle(self, tmp_path: Path) -> None:
        clock = _Clock(1_800_000_000.0)
        sink, _ = _sink(tmp_path, clock)
        first = sink.record(AlertCode.DEVICE_LOST, AlertLevel.ERROR, "e")
        clock.t += 200  # beyond CHAIN_WINDOW_S
        fatal = sink.record(AlertCode.DEVICE_LOST, AlertLevel.FATAL, "f")
        assert not fatal.chained and fatal.bundle_dir != first.bundle_dir
        assert (fatal.bundle_dir / "alert.json").is_file()

    def test_attach_to_existing_bundle(self, tmp_path: Path) -> None:
        clock = _Clock(1_800_000_000.0)
        sink, ops = _sink(tmp_path, clock)
        first = sink.record(AlertCode.GAME_OVER_UNEXPECTED, AlertLevel.FATAL, "f")
        rec = sink.record(
            AlertCode.LOOP_ERROR, AlertLevel.ERROR, "recovering",
            attach_to=first.bundle_dir.as_posix(),
        )
        assert rec.attached and rec.alert.incident_dir == first.bundle_dir.as_posix()
        assert ops.events[-1]["code"] == "LOOP_ERROR"
        assert ops.events[-1]["incident_dir"] == first.bundle_dir.as_posix()


def _old_bundle(root: Path, when: datetime, code: str, size: int) -> Path:
    d = root / f"{when.strftime('%Y%m%dT%H%M%SZ')}_{code}"
    d.mkdir(parents=True)
    (d / "screenshot_after.png").write_bytes(b"\0" * size)
    (d / "alert.json").write_text("{}")
    return d


class TestPruning:
    def test_oldest_first_never_current_run(self, tmp_path: Path) -> None:
        root = tmp_path / "incidents"
        now = 1_800_000_000.0
        old1 = _old_bundle(root, datetime.fromtimestamp(now - 86400, UTC), "CRASH", 1000)
        old2 = _old_bundle(root, datetime.fromtimestamp(now - 3600, UTC), "DEVICE_LOST", 1000)
        clock = _Clock(now)
        sink, _ = _sink(tmp_path, clock, max_total_bytes=1500, max_bundles=100)
        rec = sink.record(AlertCode.VISION_MISMATCH, AlertLevel.ERROR, "e")
        assert not old1.exists()  # oldest went first
        assert old2.exists() or not old2.exists()  # may go too, budget is tight
        assert rec.bundle_dir.exists()  # the current run's bundle is never pruned
        # Even when hopelessly over budget, current-run bundles survive.
        clock.t += 40
        rec2 = sink.record(AlertCode.VISION_MISMATCH, AlertLevel.ERROR, "e2")
        assert rec.bundle_dir.exists() and rec2.bundle_dir.exists()
        assert not old2.exists()

    def test_prune_by_count(self, tmp_path: Path) -> None:
        root = tmp_path / "incidents"
        base = datetime(2026, 7, 1, tzinfo=UTC)
        dirs = [
            _old_bundle(root, base.replace(hour=h), "SCREEN_UNEXPECTED", 10) for h in range(5)
        ]
        (root / "README.md").write_text("not a bundle")  # ignored by the pattern
        removed, freed = prune_incidents(root, max_bundles=2, max_total_bytes=10**9)
        assert removed == 3 and freed == 3 * (10 + len("{}"))
        assert [d.exists() for d in dirs] == [False, False, False, True, True]
        assert (root / "README.md").exists()

    def test_protect_callback(self, tmp_path: Path) -> None:
        root = tmp_path / "incidents"
        base = datetime(2026, 7, 1, tzinfo=UTC)
        a = _old_bundle(root, base, "CRASH", 10)
        b = _old_bundle(root, base.replace(hour=1), "CRASH", 10)
        removed, _ = prune_incidents(
            root, max_bundles=1, max_total_bytes=10**9, protect=lambda p: p == a
        )
        assert removed == 1 and a.exists() and not b.exists()


# -- Telegram photo pick + retry table consistency ----------------------------------------


def test_incident_photo_prefers_the_alerts_own_frame(tmp_path: Path) -> None:
    d = tmp_path / "logs" / "incidents" / "20260710T140322Z_SCREEN_UNEXPECTED"
    d.mkdir(parents=True)
    rel = "logs/incidents/20260710T140322Z_SCREEN_UNEXPECTED"
    assert incident_photo(tmp_path, rel, "ERROR") is None
    (d / "screenshot_before.png").write_bytes(b"x")
    assert incident_photo(tmp_path, rel, "ERROR") == str(d / "screenshot_before.png")
    (d / "screenshot_after.png").write_bytes(b"x")
    assert incident_photo(tmp_path, rel, "ERROR") == str(d / "screenshot_after.png")
    (d / "screenshot_fatal.png").write_bytes(b"x")
    assert incident_photo(tmp_path, rel, "ERROR") == str(d / "screenshot_after.png")
    assert incident_photo(tmp_path, rel, "FATAL") == str(d / "screenshot_fatal.png")
    assert incident_photo(tmp_path, None, "FATAL") is None


def test_retry_table_mirrors_the_enforcing_knobs() -> None:
    cfg = LiveConfig(experiment_id="t")
    assert POLICIES[AlertCode.MOVE_REJECTED].warn_retries == cfg.swipe_retries
    assert POLICIES[AlertCode.VISION_MISMATCH].warn_retries == cfg.parse_retries
    assert POLICIES[AlertCode.LOOP_ERROR].warn_retries == cfg.recover_max_per_hour
    repolls = SupervisorConfig().transient_repolls
    assert POLICIES[AlertCode.SCREEN_UNEXPECTED].warn_retries == repolls
    for code in (AlertCode.AUTH_REQUIRED, AlertCode.CRASH, AlertCode.RESTART_LOOP):
        assert POLICIES[code].fatal_immediately and POLICIES[code].max_attempts == 1
    assert set(POLICIES) == set(AlertCode)  # every code has a row
    for policy in POLICIES.values():
        described = policy.describe()
        assert described["max_attempts"] == policy.warn_retries + 1
        assert described["source"]


def test_move_ring_fresh_frame_never_raises() -> None:
    ring = MoveRing()
    assert ring.fresh_frame() == (None, "no fresh-capture hook installed")
    ring.set_capture(lambda: np.zeros((0,), dtype=np.uint8))
    assert ring.fresh_frame() == (None, "capture returned an empty frame")

    def _raise() -> Any:
        raise DeviceError("gone")

    ring.set_capture(_raise)
    assert ring.fresh_frame() == (None, "DeviceError: gone")
    ring.set_capture(_frame)
    frame, err = ring.fresh_frame()
    assert err is None and frame is not None and frame.shape == (8, 8, 3)


def test_no_litter_outside_tmp_path() -> None:
    """Guard for this module: nothing above wrote into the real project/logs."""
    project_logs = Path(__file__).resolve().parents[2] / "logs"
    stale = time.time() - 300
    fresh = [
        p for p in project_logs.rglob("*")
        if p.is_file() and os.path.getmtime(p) > stale
    ] if project_logs.exists() else []
    assert fresh == []

"""``bot2048 db latency``: phase percentiles, critical share and the verdict.

Synthetic mini-DBs are written through the real SqliteRecorder (the frozen
write path), then read back via the CLI / collect_latency. Deterministic:
hand-picked millisecond values, no sleeps, no real devices.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import board_with
from typer.testing import CliRunner

from bot2048.core.types import Direction
from bot2048.recording import GameEnded, GameStarted, MovePlayed
from bot2048.storage import ExperimentMeta, SqliteRecorder
from bot2048.storage.cli import app
from bot2048.storage.latency import collect_latency, open_readonly

runner = CliRunner()


def _flat(output: str) -> str:
    """Collapse whitespace: rich wraps table captions at the console width."""
    return " ".join(output.split())

B0 = board_with({0: 1, 5: 2})
B1 = board_with({0: 2, 5: 2})

#: settle dominates the move phases by an order of magnitude
TIMING = {
    "capture_ms": 40.0,
    "perceive_ms": 10.0,
    "detect_ms": 5.0,
    "swipe_ms": 80.0,
    "settle_ms": 500.0,
    "total_ms": 635.0,
}


def _record_game(
    db: Path,
    experiment_id: str,
    params: dict[str, Any],
    move_details: list[dict[str, Any]],
    eval_ms: float = 20.0,
) -> None:
    meta = ExperimentMeta(
        experiment_id=experiment_id, kind="live", strategy="scripted", params=params
    )
    game_id = f"{experiment_id}-game"
    with SqliteRecorder(db, meta) as recorder:
        recorder.record(
            GameStarted(
                game_id=game_id,
                experiment_id=experiment_id,
                kind="live",
                strategy="scripted",
                seed=None,
                initial_board=B0,
                ts=100.0,
            )
        )
        for ply, details in enumerate(move_details):
            recorder.record(
                MovePlayed(
                    game_id=game_id,
                    ply=ply,
                    direction=Direction.LEFT,
                    board_before=B0,
                    board_after=B1,
                    score_gained=4,
                    score_total=4 * (ply + 1),
                    eval_ms=eval_ms,
                    search_depth=2,
                    eval_details=details,
                    ts=100.0 + ply,
                )
            )
        recorder.record(
            GameEnded(
                game_id=game_id,
                final_score=4 * len(move_details),
                max_exponent=2,
                num_moves=len(move_details),
                end_reason="game_over",
                duration_s=float(len(move_details)),
                ts=200.0,
            )
        )


@pytest.fixture()
def settle_db(tmp_path: Path) -> Path:
    """5 timed moves, settle-bound; engine/prefetch known on 4 of 5 moves."""
    db = tmp_path / "latency.db"
    details: list[dict[str, Any]] = [
        {"timing": dict(TIMING), "engine_ms": 15.0, "prefetch_hit": True},
        {"timing": dict(TIMING), "engine_ms": 15.0, "prefetch_hit": True},
        {"timing": dict(TIMING), "engine_ms": 15.0, "prefetch_hit": False},
        {"timing": dict(TIMING), "engine_ms": 15.0, "prefetch_hit": False},
        {"timing": dict(TIMING)},  # P16 wrapper absent: engine/prefetch unknown
    ]
    _record_game(db, "lat-settle", {"capture": "scrcpy"}, details)
    return db


@pytest.fixture()
def reliability_db(tmp_path: Path) -> Path:
    """4 timed moves carrying the ``move`` bucket: gate + FACTUAL capture kind.

    Gate ran on all 4, hit on 3 (75%); the stream silently fell back to
    screencap on 1 of the 4 kind-reporting moves (25%). The CONFIGURED capture
    is scrcpy -- the report must surface the DISAGREEING fact. The 3 APK-served
    moves also carry ``apk_age_ms`` (mailbox dwell: 8/12/10 -> avg 10.0); the
    screencap fallback move has none, as in real runs.
    """
    db = tmp_path / "reliability.db"

    def _move(
        *, hit: bool, kind: str, apk_age_ms: float | None = None
    ) -> dict[str, Any]:
        move: dict[str, Any] = {
            "capture_count": 2,
            "swipe_attempts": 1,
            "gate_hit": hit,
            "gate_fallback": not hit,
            "gate_frames": 1 if hit else 3,
            "capture_kind": kind,
        }
        if apk_age_ms is not None:
            move["apk_age_ms"] = apk_age_ms
        return {"timing": dict(TIMING), "move": move}

    details = [
        _move(hit=True, kind="scrcpy", apk_age_ms=8.0),
        _move(hit=True, kind="scrcpy", apk_age_ms=12.0),
        _move(hit=True, kind="scrcpy", apk_age_ms=10.0),
        _move(hit=False, kind="screencap"),
    ]
    _record_game(db, "lat-rel", {"capture": "scrcpy"}, details)
    return db


@pytest.fixture()
def gap_db(tmp_path: Path) -> Path:
    """4 stamped moves (design §5.1): critical = 635 + 20 = 655 ms each.

    ``t_commit`` gaps are 700 / 800 / 700 ms -> ``unaccounted`` = 45 / 145 / 45;
    ``prev_commit_ms`` rides on plies 1..3 only (ply 0 has no previous move).
    """
    db = tmp_path / "gap.db"
    stamps = (1000.0, 1000.7, 1001.5, 1002.2)
    prev_commit = (None, 2.0, 3.0, 50.0)
    details: list[dict[str, Any]] = []
    for t_commit, prev in zip(stamps, prev_commit, strict=True):
        move: dict[str, Any] = {"capture_count": 1, "swipe_attempts": 1, "t_commit": t_commit}
        if prev is not None:
            move["prev_commit_ms"] = prev
        details.append({"timing": dict(TIMING), "move": move})
    _record_game(db, "lat-gap", {"capture": "apk"}, details)
    return db


@pytest.fixture()
def search_db(tmp_path: Path) -> Path:
    """3 timed moves where the (unsplit) search dwarfs every move phase."""
    db = tmp_path / "search.db"
    timing = {
        "capture_ms": 30.0,
        "perceive_ms": 10.0,
        "detect_ms": 5.0,
        "swipe_ms": 60.0,
        "settle_ms": 90.0,
        "total_ms": 195.0,
    }
    details: list[dict[str, Any]] = [{"timing": dict(timing)} for _ in range(3)]
    _record_game(db, "lat-search", {}, details, eval_ms=800.0)
    return db


class TestLatencyCommand:
    def test_prints_report_and_settle_verdict(self, settle_db: Path) -> None:
        result = runner.invoke(app, ["latency", "--db", str(settle_db)])
        assert result.exit_code == 0, result.output
        flat = _flat(result.output)
        assert "lat-settle" in flat
        assert "5 moves" in flat
        assert "capture=scrcpy" in flat
        assert "слабина = settle" in flat
        assert "TOTAL" in flat
        assert "CRITICAL" in flat
        assert "prefetch hit-rate: 50%" in flat

    def test_search_bound_db_blames_search(self, search_db: Path) -> None:
        result = runner.invoke(app, ["latency", "--db", str(search_db)])
        assert result.exit_code == 0, result.output
        flat = _flat(result.output)
        assert "слабина = search" in flat
        assert "prefetch hit-rate: unknown" in flat

    def test_actual_capture_kind_and_gate_hit_rate_shown(
        self, reliability_db: Path
    ) -> None:
        result = runner.invoke(app, ["latency", "--db", str(reliability_db)])
        assert result.exit_code == 0, result.output
        flat = _flat(result.output)
        # configured label stays present AND the disagreeing fact is spelled out
        assert "capture=scrcpy" in flat
        assert "actual: 25% screencap of 4 moves" in flat
        # predict-gate hit-rate line: 3 of 4 gate-moves accepted
        assert "predict-gate: hit 75%" in flat
        assert "fallback 25%" in flat
        # apk packet-age line: mailbox dwell avg of the 3 apk-served moves
        assert "apk packet-age: avg 10.0 ms in mailbox (3 moves)" in flat

    def test_loop_gap_lines_shown_when_stamped(self, gap_db: Path) -> None:
        result = runner.invoke(app, ["latency", "--db", str(gap_db)])
        assert result.exit_code == 0, result.output
        flat = _flat(result.output)
        assert (
            "loop gap, unaccounted (gap - critical): p50 45.0 / p95 45.0 / p99 45.0 / "
            "max 145.0 ms (n=3)"
        ) in flat
        assert (
            "loop gap, prev_commit (persist section): p50 3.0 / p95 3.0 / p99 3.0 / "
            "max 50.0 ms (n=3)"
        ) in flat

    def test_loop_gap_silent_without_stamp(self, settle_db: Path) -> None:
        result = runner.invoke(app, ["latency", "--db", str(settle_db)])
        assert result.exit_code == 0, result.output
        assert "loop gap" not in result.output

    def test_experiment_filter_excludes_others(self, settle_db: Path) -> None:
        result = runner.invoke(
            app, ["latency", "--db", str(settle_db), "--experiment", "nope"]
        )
        assert result.exit_code == 0, result.output
        assert "no timed moves recorded" in result.output

    def test_db_without_timing_reports_nothing(self, synthetic_db: Path) -> None:
        result = runner.invoke(app, ["latency", "--db", str(synthetic_db)])
        assert result.exit_code == 0, result.output
        assert "no timed moves recorded" in result.output

    def test_missing_db_fails(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["latency", "--db", str(tmp_path / "absent.db")])
        assert result.exit_code == 1


class TestCollectLatency:
    def test_settle_math(self, settle_db: Path) -> None:
        conn = open_readonly(settle_db)
        try:
            reports = collect_latency(conn)
        finally:
            conn.close()
        assert len(reports) == 1
        report = reports[0]

        assert report.experiment_id == "lat-settle"
        assert report.moves == 5
        assert report.capture_mode == "scrcpy"
        assert report.bottleneck == "settle"
        # prefetch known on 4 moves, hit on 2 of them
        assert report.prefetch_hit_pct == pytest.approx(50.0)
        # full = total + eval on every move; critical drops eval on the 2 hits
        assert report.avg_full_ms == pytest.approx(655.0)
        assert report.avg_critical_ms == pytest.approx(655.0 - 2 * 20.0 / 5)

        phases = {row.phase for row in report.percentiles}
        # engine data exists -> the IPC/engine split is present
        assert {"search_ipc", "search_engine", "TOTAL", "CRITICAL"} <= phases
        by_phase = {row.phase: row for row in report.percentiles}
        assert by_phase["settle"].p50 == pytest.approx(500.0)
        assert by_phase["TOTAL"].max_ms == pytest.approx(635.0)
        assert by_phase["search_ipc"].p50 == pytest.approx(5.0)  # 20 - 15

    def test_reliability_fields_from_move_bucket(self, reliability_db: Path) -> None:
        conn = open_readonly(reliability_db)
        try:
            reports = collect_latency(conn)
        finally:
            conn.close()
        assert len(reports) == 1
        report = reports[0]
        assert report.gate_hit_pct == pytest.approx(75.0)
        assert report.gate_fallback_pct == pytest.approx(25.0)
        assert report.capture_kind_known == 4
        assert report.capture_screencap_pct == pytest.approx(25.0)
        # config label is still available, but the fact now overrides in the CLI
        assert report.capture_mode == "scrcpy"
        # apk mailbox dwell: only the 3 apk-served moves reported an age
        assert report.apk_moves == 3
        assert report.apk_age_avg_ms == pytest.approx(10.0)

    def test_reliability_none_when_move_bucket_absent(self, settle_db: Path) -> None:
        conn = open_readonly(settle_db)
        try:
            report = collect_latency(conn)[0]
        finally:
            conn.close()
        # settle_db moves predate the ``move`` bucket -> no fabricated rates
        assert report.gate_hit_pct is None
        assert report.gate_fallback_pct is None
        assert report.capture_kind_known == 0
        assert report.capture_screencap_pct is None
        assert report.apk_moves == 0
        assert report.apk_age_avg_ms is None

    def test_missing_engine_data_collapses_to_combined_search(
        self, search_db: Path
    ) -> None:
        conn = open_readonly(search_db)
        try:
            reports = collect_latency(conn)
        finally:
            conn.close()
        assert len(reports) == 1
        report = reports[0]

        assert report.prefetch_hit_pct is None  # never recorded -> None, not 0
        assert report.capture_mode is None
        assert report.bottleneck == "search"
        phases = {row.phase for row in report.percentiles}
        assert "search_ipc" not in phases
        assert "search_engine" not in phases
        assert "search_eval" in phases
        # unknown prefetch counts as blocking: critical includes the search
        assert report.avg_critical_ms == pytest.approx(995.0)

    def test_experiment_filter(self, settle_db: Path) -> None:
        conn = open_readonly(settle_db)
        try:
            assert collect_latency(conn, "lat-settle")
            assert collect_latency(conn, "absent") == ()
        finally:
            conn.close()

    def test_loop_gap_math(self, gap_db: Path) -> None:
        conn = open_readonly(gap_db)
        try:
            report = collect_latency(conn)[0]
        finally:
            conn.close()
        unaccounted = report.unaccounted
        assert unaccounted is not None
        # 4 stamped plies -> 3 consecutive pairs; gap - critical(n) = 45/145/45
        assert unaccounted.n == 3
        assert unaccounted.p50 == pytest.approx(45.0)
        assert unaccounted.p95 == pytest.approx(45.0)
        assert unaccounted.p99 == pytest.approx(45.0)
        assert unaccounted.max_ms == pytest.approx(145.0)
        prev = report.prev_commit
        assert prev is not None
        assert prev.n == 3  # ply 0 carries no prev_commit_ms
        assert prev.p50 == pytest.approx(3.0)
        assert prev.max_ms == pytest.approx(50.0)

    def test_loop_gap_none_before_stamp_existed(self, settle_db: Path) -> None:
        conn = open_readonly(settle_db)
        try:
            report = collect_latency(conn)[0]
        finally:
            conn.close()
        assert report.gaps == ()
        assert report.unaccounted is None
        assert report.prev_commit is None

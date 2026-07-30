"""Reliability telemetry: predict-gate, vision fallback, counts, capture kind.

research/telemetry_review.md top-5 (M1-M4, N6). All additive: the per-move
signals ride in ``eval_details["move"]`` / ``["vision"]`` (free-form neighbors
of the frozen ``timing`` bucket) and the per-game aggregates in the
``GAME_OVER`` / ``MOVE_ABORTED`` ops ``details_json``. Everything runs against
the in-memory fake phone with the deterministic fake clock -- zero real delays.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from bot2048.core.types import Direction, Spawn
from bot2048.recording import MovePlayed
from bot2048.runner.live import LiveConfig
from tests.runner.conftest import (
    NEARLY_DEAD_BOARD,
    FakePhone,
    ScriptedStrategy,
    make_harness,
)


class _FakeOps:
    """Captures ``record_event`` calls (matches OpsRepository's signature)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

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
        self.events.append({"code": code, "message": message, "details": details})


def _moves(harness: Any) -> list[MovePlayed]:
    return [e for e in harness.recorder.events if isinstance(e, MovePlayed)]


def _events(ops: _FakeOps, code: str) -> list[dict[str, Any]]:
    return [e for e in ops.events if e["code"] == code]


def _play_one_move(tmp_path: Path, phone: FakePhone, **kwargs: Any) -> Any:
    h = make_harness(
        tmp_path,
        phone,
        strategy=ScriptedStrategy([Direction.RIGHT]),
        config=LiveConfig(experiment_id="t", max_games=1),
        **kwargs,
    )
    return h


class TestMoveBucket:
    def test_records_predict_gate_and_counts(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = _play_one_move(tmp_path, phone)
        h.runner.run()

        moves = _moves(h)
        assert len(moves) == 1
        move = moves[0].eval_details["move"]
        # the happy path is accepted by the predict-gate on the first frame
        assert move["gate_hit"] is True
        assert move["gate_fallback"] is False
        assert move["gate_frames"] >= 1
        assert move["swipe_attempts"] == 1
        assert move["capture_count"] >= 1
        # a bare fake device cannot report a FACTUAL capture kind -> key omitted
        assert "capture_kind" not in move

    def test_capture_kind_reflects_the_actual_source(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        # A device that reports it fell back off the stream to screencap.
        phone.last_capture_kind = lambda: "screencap"  # type: ignore[attr-defined]
        h = _play_one_move(tmp_path, phone)
        h.runner.run()

        move = _moves(h)[0].eval_details["move"]
        assert move["capture_kind"] == "screencap"

    def test_vision_bucket_counts_suspect_cells(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            suspect_cells={0, 5},  # color-fallback / low-confidence reads
        )
        h = _play_one_move(tmp_path, phone, read_cells=phone.read_cells)
        h.runner.run()

        vision = _moves(h)[0].eval_details["vision"]
        assert vision["suspect"] == 2
        assert vision["suspect_mask"] == (1 << 0) | (1 << 5)
        assert len(vision["suspect_exps"]) == 2

    def test_vision_bucket_absent_without_read_cells(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = _play_one_move(tmp_path, phone)  # no read_cells injected
        h.runner.run()
        assert "vision" not in _moves(h)[0].eval_details


class TestPerGameAggregates:
    def test_game_over_ships_reliability_summary(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = _play_one_move(tmp_path, phone)
        ops = _FakeOps()
        h.runner.ops = ops  # type: ignore[assignment]
        h.runner.run()

        game_overs = _events(ops, "GAME_OVER")
        assert len(game_overs) == 1
        details = game_overs[0]["details"]
        assert details is not None
        # one gate-accepted move -> 100% hit
        assert details["gate_moves"] == 1
        assert details["gate_hit_pct"] == 100.0
        assert details["vision_fallback_moves"] == 0
        assert details["swipe_retry_total"] == 0
        assert details["capture_screencap_moves"] == 0
        assert details["mean_move_ms"] > 0.0
        assert "p95_move_ms" in details
        assert details["moves_per_s"] > 0.0
        assert "score_per_s" in details
        # the board never reached 1024/2048, so those plies stay unset
        assert "first_1024_ply" not in details

    def test_swipe_retries_counted_in_summary(self, tmp_path: Path) -> None:
        # First swipe is dropped, the retry registers -> swipe_retry_total == 1.
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            reject_swipes=1,
        )
        h = _play_one_move(tmp_path, phone)
        ops = _FakeOps()
        h.runner.ops = ops  # type: ignore[assignment]
        h.runner.run()

        details = _events(ops, "GAME_OVER")[0]["details"]
        assert details is not None
        assert details["swipe_retry_total"] == 1


class TestAbortTelemetry:
    def test_move_rejected_emits_timing_and_counts(self, tmp_path: Path) -> None:
        phone = FakePhone(board=NEARLY_DEAD_BOARD, reject_swipes=99)
        h = _play_one_move(tmp_path, phone)
        ops = _FakeOps()
        h.runner.ops = ops  # type: ignore[assignment]
        h.runner.run()

        aborted = _events(ops, "MOVE_ABORTED")
        assert len(aborted) == 1
        details = aborted[0]["details"]
        assert details is not None
        assert details["end_reason"] == "move_rejected"
        # the aborted move's timing is preserved (it never reaches ``moves``)
        assert "timing" in details
        assert details["timing"]["total_ms"] > 0.0
        # all swipe attempts (1 + swipe_retries) are visible on the failed move
        assert details["move"]["swipe_attempts"] == 5
        # no MovePlayed was recorded for an unverified move
        assert _moves(h) == []

"""Research mode: fresh-start restart flow + spawn measurement via the diff."""

from __future__ import annotations

import random
import sqlite3
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from bot2048.core.types import Spawn
from bot2048.device import ui_map
from bot2048.device.humanizer import Humanizer
from bot2048.device.supervisor import Supervisor, SupervisorConfig
from bot2048.recording import GameEnded, GameStarted, ListRecorder, MovePlayed, TileSpawned
from bot2048.runner.alerts import AlertWriter
from bot2048.runner.research import ResearchConfig, ResearchRunner, make_policy
from tests.runner.conftest import FRESH_BOARD, GRID_BBOX, FakePhone


@pytest.fixture(autouse=True)
def _isolate_runtime_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ResearchRunner builds its LiveRunner with the DEFAULT relative artifact
    roots (``logs/game-screens``, ``logs/debug-captures``); run from tmp_path so
    the end-of-game PNGs never land in the real ``project/logs/``."""
    monkeypatch.chdir(tmp_path)


def _make_research(
    tmp_path: Path,
    phone: FakePhone,
    config: ResearchConfig,
    recorder: Any,
) -> ResearchRunner:
    alerts = AlertWriter(tmp_path / "alerts.jsonl")
    supervisor = Supervisor(
        phone,
        phone.detect,
        alerts,
        SupervisorConfig(incidents_root=tmp_path / "incidents"),
        sleep=lambda _s: None,
    )
    return ResearchRunner(
        device=phone,
        detect_state=phone.detect,
        read_board=phone.read_board,
        recorder=recorder,
        supervisor=supervisor,
        alerts=alerts,
        grid_bbox=GRID_BBOX,
        humanizer=Humanizer(rng=random.Random(11)),
        config=config,
        pause_flag=tmp_path / "pause.flag",
        rng=random.Random(42),
        sleep=lambda _s: None,
        clock=lambda: 0.0,
    )


class TestResearchMode:
    def test_fresh_start_and_scripted_spawns_recorded(self, tmp_path: Path) -> None:
        script = deque([Spawn(cell=8, exponent=2), Spawn(cell=3, exponent=1)])
        phone = FakePhone(board=(1, 1, 0, 0) + (0,) * 12, spawn_script=script)
        recorder = ListRecorder()
        runner = _make_research(
            tmp_path,
            phone,
            ResearchConfig(experiment_id="spawn-research", games=1),
            recorder,
        )
        runner.run()

        # fresh_start: the session begins with the calibrated restart flow
        assert phone.taps[0] == ui_map.NEW_GAME
        assert ui_map.CONFIRM_YES in phone.taps

        started = [e for e in recorder.events if isinstance(e, GameStarted)]
        assert len(started) == 1
        assert started[0].initial_board == FRESH_BOARD
        assert started[0].experiment_id == "spawn-research"
        assert started[0].strategy == "random"

        # initial tiles: negative plies, deterministic cell order
        initial = [
            e for e in recorder.events if isinstance(e, TileSpawned) and e.ply < 0
        ]
        assert [(e.ply, e.spawn.cell) for e in initial] == [(-1, 0), (-2, 15)]

        # every recorded move-spawn equals what the phone actually spawned,
        # in order -- the diff measured the ground truth exactly
        recorded = [
            e.spawn for e in recorder.events if isinstance(e, TileSpawned) and e.ply >= 0
        ]
        assert recorded == phone.spawn_log
        assert len(recorded) > 0
        # the scripted prefix was consumed first
        assert recorded[0] == Spawn(cell=8, exponent=2)

        ended = [e for e in recorder.events if isinstance(e, GameEnded)]
        assert len(ended) == 1
        assert ended[0].end_reason == "game_over"  # random play to engine death
        moves = [e for e in recorder.events if isinstance(e, MovePlayed)]
        assert len(moves) == ended[0].num_moves == len(recorded)

    def test_unknown_policy_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown move_policy"):
            make_policy("expectimax-deep")


class TestSqliteRecorderIntegration:
    def test_live_game_lands_in_sqlite(self, tmp_path: Path) -> None:
        from bot2048.storage.sqlite_recorder import ExperimentMeta, SqliteRecorder

        script = deque([Spawn(cell=8, exponent=1)])
        phone = FakePhone(board=(1, 1, 0, 0) + (0,) * 12, spawn_script=script)
        db_path = tmp_path / "games.db"
        recorder = SqliteRecorder(
            db_path,
            ExperimentMeta(experiment_id="spawn-research", kind="live", strategy="random"),
        )
        runner = _make_research(
            tmp_path,
            phone,
            ResearchConfig(experiment_id="spawn-research", games=1),
            recorder,
        )
        try:
            runner.run()
        finally:
            recorder.close()

        conn = sqlite3.connect(db_path)
        try:
            games = conn.execute(
                "SELECT experiment_id, end_reason, num_moves FROM games"
            ).fetchall()
            assert len(games) == 1
            assert games[0][0] == "spawn-research"
            assert games[0][1] == "game_over"
            n_moves = conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0]
            n_spawns = conn.execute("SELECT COUNT(*) FROM spawns WHERE ply >= 0").fetchone()[0]
            n_initial = conn.execute("SELECT COUNT(*) FROM spawns WHERE ply < 0").fetchone()[0]
            assert n_moves == games[0][2] == n_spawns
            assert n_initial == 2
        finally:
            conn.close()

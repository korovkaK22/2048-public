"""Game-end statistics: final-board tile histogram + last-move screenshot.

Every game finalization (game over, win, or any abort) funnels through
``LiveRunner._end_game``, which now (1) saves the last captured frame to
``<screens_root>/<game_id>_<end_reason>.png`` and (2) computes a histogram of
the final board and ships it both to the ``live.game_ended`` log line and the
ops ``live_events.details_json`` telemetry field. These tests exercise the pure
histogram, the telemetry payload, the screenshot path, and the graceful
no-frame path -- all against the in-memory fake phone, zero real delays.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from bot2048.core.types import Board, Direction, Spawn
from bot2048.recording import GameStarted
from bot2048.runner.live import LiveConfig, _board_stats
from tests.runner.conftest import (
    DEAD_BOARD,
    NEARLY_DEAD_BOARD,
    FakePhone,
    ScriptedStrategy,
    make_harness,
)

# -- the measurement instrument (pure) -----------------------------------------


class TestBoardStats:
    def test_counts_values_empty_highest_and_sum(self) -> None:
        # two 1024s (exp 10), one 512 (exp 9), the rest empty -- the real 25264
        # end board shape (two 1024 on a jammed board).
        board: Board = (10, 10, 9) + (0,) * 13
        stats = _board_stats(board)
        assert stats.histogram == {1024: 2, 512: 1}
        assert stats.highest == 1024
        assert stats.empty == 13
        assert stats.total == 1024 + 1024 + 512
        # keys are ordered by descending tile value
        assert list(stats.histogram) == [1024, 512]

    def test_full_board_of_mixed_low_tiles(self) -> None:
        # DEAD_BOARD is eight 2s (exp 1) and eight 4s (exp 2), no empties.
        stats = _board_stats(DEAD_BOARD)
        assert stats.histogram == {4: 8, 2: 8}
        assert stats.highest == 4
        assert stats.empty == 0
        assert stats.total == 8 * 4 + 8 * 2

    def test_empty_board(self) -> None:
        stats = _board_stats((0,) * 16)
        assert stats.histogram == {}
        assert stats.highest == 0
        assert stats.empty == 16
        assert stats.total == 0


# -- telemetry: the histogram reaches the ops details_json ---------------------


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


def _game_over_events(ops: _FakeOps) -> list[dict[str, Any]]:
    return [e for e in ops.events if e["code"] == "GAME_OVER"]


class TestFinalHistogramTelemetry:
    def test_game_over_ships_tile_histogram(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        ops = _FakeOps()
        h.runner.ops = ops  # type: ignore[assignment]
        h.runner.run()

        game_overs = _game_over_events(ops)
        assert len(game_overs) == 1
        details = game_overs[0]["details"]
        assert details is not None
        # The one RIGHT move jams the board into DEAD_BOARD (eight 4s, eight 2s).
        assert details["tile_histogram"] == {4: 8, 2: 8}
        assert details["highest"] == 4
        assert details["empty"] == 0
        assert details["tile_sum"] == 8 * 4 + 8 * 2
        assert details["end_reason"] == "game_over"

    def test_aborted_cap_still_ships_histogram(self, tmp_path: Path) -> None:
        # max_moves_per_game=0 -> the game ends 'aborted' before any move, so
        # the final board is the initial NEARLY_DEAD_BOARD (one empty cell).
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, max_moves_per_game=0),
        )
        ops = _FakeOps()
        h.runner.ops = ops  # type: ignore[assignment]
        h.runner.run()

        game_overs = _game_over_events(ops)
        assert len(game_overs) == 1
        details = game_overs[0]["details"]
        assert details is not None
        assert details["end_reason"] == "aborted"
        assert details["tile_histogram"] == {4: 8, 2: 7}
        assert details["highest"] == 4
        assert details["empty"] == 1


# -- the screenshot of the last move -------------------------------------------


class TestGameScreenSaved:
    def test_screen_written_to_expected_path(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        saved: list[tuple[Path, Any]] = []
        monkeypatch.setattr(
            "bot2048.runner.incidents.write_png",
            lambda path, image: saved.append((path, image)),
        )
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        h.runner.run()

        game_id = next(
            e.game_id for e in h.recorder.events if isinstance(e, GameStarted)
        )
        # One artifact now: the last game frame. The final board travels in the
        # notification text as a monospace grid, no separate rendered PNG.
        by_path = {path: image for path, image in saved}
        screen = tmp_path / "game-screens" / f"{game_id}_game_over.png"
        board = tmp_path / "game-screens" / f"{game_id}_final_board.png"
        assert screen in by_path and by_path[screen] is not None
        assert board not in by_path
        # the directory is created before the write
        assert (tmp_path / "game-screens").is_dir()

    def test_aborted_screen_uses_end_reason_suffix(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        saved: list[Path] = []
        monkeypatch.setattr(
            "bot2048.runner.incidents.write_png",
            lambda path, image: saved.append(path),
        )
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, max_moves_per_game=0),
        )
        h.runner.run()

        names = {p.name for p in saved}
        # The last-frame artifact carries the end_reason suffix; it is now the
        # ONLY artifact (no rendered final-board PNG anymore).
        assert any(n.endswith("_aborted.png") for n in names)
        assert not any(n.endswith("_final_board.png") for n in names)

    def test_saved_frame_is_fresh_terminal_capture_not_stale(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Regression: APK-mode end screenshot must NOT be the startup frame.

        In APK-capture mode per-move perception reads 22-byte packets, so the
        gate's ``last_frame`` stays whatever full frame was parsed LAST -- the
        game's STARTUP board. The saved end-of-game screen must instead be a
        FRESH screencap of the terminal screen. The fake phone tags every new
        frame with an incrementing fill value, so fresh vs stale is exact.
        """
        saved: list[tuple[Path, Any]] = []
        monkeypatch.setattr(
            "bot2048.runner.incidents.write_png",
            lambda path, image: saved.append((path, image)),
        )
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        # The stale startup frame behind the latest parse (APK-mode reality).
        startup = np.zeros((8, 8, 3), dtype=np.uint8)
        h.runner._gate._last_frame = startup

        path = h.runner._save_game_screen("game-abc", "win_2048")

        assert path is not None
        ((saved_path, image),) = saved
        assert saved_path == path
        # Fresh terminal capture, not the stale startup frame.
        assert not np.array_equal(image, startup)
        assert image[0, 0, 0] == phone._frame_no % 251  # the LATEST device frame

    def test_capture_failure_falls_back_to_last_parsed_frame(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        saved: list[tuple[Path, Any]] = []
        monkeypatch.setattr(
            "bot2048.runner.incidents.write_png",
            lambda path, image: saved.append((path, image)),
        )
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        stale = np.full((8, 8, 3), 7, dtype=np.uint8)
        h.runner._gate._last_frame = stale
        # The end-of-game screencap fails: a stale postmortem beats none.
        phone.transient_errors.append(RuntimeError("adb: device gone (fake)"))

        path = h.runner._save_game_screen("game-abc", "game_over")

        assert path is not None
        ((_, image),) = saved
        assert np.array_equal(image, stale)


class TestMissingFrameIsGraceful:
    def test_no_frame_does_not_raise_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        called: list[Path] = []
        monkeypatch.setattr(
            "bot2048.runner.incidents.write_png",
            lambda path, image: called.append(path),
        )
        phone = FakePhone(board=NEARLY_DEAD_BOARD)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
        )
        # Simulate a vanished device: the fresh end-of-game screencap fails AND
        # there is no frame behind the latest parse to fall back to.
        # (``last_frame`` lives on the ObservationGate now, R4.)
        phone.transient_errors.append(RuntimeError("adb: device gone (fake)"))
        h.runner._gate._last_frame = None
        h.runner._save_game_screen("game-xyz", "device_lost")

        assert called == []  # nothing written
        assert not (tmp_path / "game-screens").exists()  # not even created

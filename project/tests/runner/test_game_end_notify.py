"""Enriched game-over / win notification: duration, board grid, stats, image.

Covers the pure formatting helpers (``format_duration``, ``_render_final_grid``,
``_format_game_end_notify``) plus the end-to-end wiring: a finished game emits a
rich text notification (stats + a monospace board grid in a code block) AND
attaches exactly ONE device frame -- all against the in-memory fake phone, no
real delays. There is no separately rendered board PNG anymore.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from bot2048.core.types import Board, Direction, Spawn
from bot2048.runner.live import (
    LiveConfig,
    _board_stats,
    _format_game_end_notify,
    _render_final_grid,
    format_duration,
)
from bot2048.storage.history import FarmPercentile
from tests.runner.conftest import NEARLY_DEAD_BOARD, FakePhone, ScriptedStrategy, make_harness

# -- format_duration -----------------------------------------------------------


class TestFormatDuration:
    def test_minutes_seconds(self) -> None:
        assert format_duration(594) == "9:54"

    def test_under_a_minute_pads_seconds(self) -> None:
        assert format_duration(7) == "0:07"

    def test_rounds_to_nearest_second(self) -> None:
        assert format_duration(594.4) == "9:54"
        assert format_duration(59.6) == "1:00"

    def test_hours_use_h_mm_ss(self) -> None:
        assert format_duration(3661) == "1:01:01"
        assert format_duration(3600) == "1:00:00"

    def test_negative_clamps_to_zero(self) -> None:
        assert format_duration(-5) == "0:00"


# -- board grid (positions, not just counts) -----------------------------------


class TestRenderFinalGrid:
    def test_positions_and_empties(self) -> None:
        # 1024 top-left, 512 next to it, a 2 at bottom-right, rest empty.
        board: Board = (10, 9, 0, 0) + (0,) * 11 + (1,)
        grid = _render_final_grid(board)
        rows = grid.splitlines()
        assert len(rows) == 4
        # Width is the widest present value ("1024" -> 4); empties render as ".".
        assert rows[0] == "1024  512    .    ."
        assert rows[3] == "   .    .    .    2"

    def test_empty_board_all_dots(self) -> None:
        rows = _render_final_grid((0,) * 16).splitlines()
        assert rows == [". . . ."] * 4


# -- full message composition --------------------------------------------------


class TestFormatGameEndNotify:
    def _board(self) -> Board:
        # two 1024s + one 512, rest empty (a realistic jammed end board).
        return (10, 10, 9) + (0,) * 13

    def test_contains_all_sections(self) -> None:
        board = self._board()
        text = _format_game_end_notify(
            game_id="0d8d8d6e-aaaa",
            end_reason="game_over",
            score=34324,
            moves=1886,
            duration_s=594,
            stats=_board_stats(board),
            board=board,
            percentile=None,
        )
        assert "🏁 Гра 0d8d8d6e — game_over" in text
        assert "Рахунок 34324" in text
        assert "9:54" in text
        assert "1886 ходів" in text
        assert "18/хід" in text  # 34324 / 1886 = 18.2 -> 18
        assert "макс. плитка 1024" in text
        # Positions grid, fenced for Telegram monospace.
        assert "```" in text
        assert "1024 1024  512" in text
        # Tile breakdown + fill/sum.
        assert "Плитки: 1024×2, 512×1" in text
        assert "Порожніх: 13" in text
        assert "сума 2560" in text
        # Baseline delta vs the farm sim mean (28.6k): 34324 - 28600 = +5724.
        assert "vs farm-базлайн 28.6k: +5724" in text
        # No percentile line when the DB gave nothing.
        assert "минулих farm-ігор" not in text

    def test_percentile_line_present_when_available(self) -> None:
        board = self._board()
        text = _format_game_end_notify(
            game_id="g",
            end_reason="game_over",
            score=34324,
            moves=1000,
            duration_s=100,
            stats=_board_stats(board),
            board=board,
            percentile=FarmPercentile(better_pct=78.0, sample_size=42),
        )
        assert "Краще за 78% минулих farm-ігор (N=42)" in text

    def test_below_baseline_shows_negative_delta(self) -> None:
        board = self._board()
        text = _format_game_end_notify(
            game_id="g",
            end_reason="aborted",
            score=10000,
            moves=500,
            duration_s=60,
            stats=_board_stats(board),
            board=board,
            percentile=None,
        )
        assert "vs farm-базлайн 28.6k: -18600" in text


# -- end-to-end wiring through the live loop -----------------------------------


class TestGameEndNotificationWiring:
    def _run_one_game_over(self, tmp_path: Path) -> object:
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
        return h

    def test_rich_text_notification_emitted(self, tmp_path: Path) -> None:
        h = self._run_one_game_over(tmp_path)
        game_over = [n for n in h.notifications if n[1] == "GAME_OVER"]  # type: ignore[attr-defined]
        assert len(game_over) == 1
        text = game_over[0][2]
        assert text.startswith("🏁 Гра ")
        assert "```" in text  # the board grid block
        assert "Плитки:" in text
        assert "Порожніх:" in text
        assert "vs farm-базлайн" in text

    def test_only_the_device_frame_image_attached(self, tmp_path: Path) -> None:
        h = self._run_one_game_over(tmp_path)
        photos = h.photo_notifications  # type: ignore[attr-defined]
        captions = {c for _lvl, _code, c, _path in photos}
        paths = [Path(p).name for _lvl, _code, _c, p in photos]
        # Exactly ONE image now: the last device frame (game screen).
        assert len(photos) == 1
        assert any("Останній кадр" in c for c in captions)
        assert any(name.endswith("_game_over.png") for name in paths)
        # The drawn "final board" PNG is gone entirely.
        assert not any("Фінальна дошка" in c for c in captions)
        assert not any(name.endswith("_final_board.png") for name in paths)
        # The file was actually written.
        for _lvl, _code, _c, p in photos:
            assert Path(p).is_file()

    def test_percentile_line_present_after_enough_history(self, tmp_path: Path) -> None:
        # Seed a real SqliteRecorder DB with >= 5 completed games, then run one
        # live game against it so ``_farm_percentile`` reads the same DB (mode=ro).
        from bot2048.storage.history import DEFAULT_MIN_GAMES
        from bot2048.storage.schema import connect, ensure_schema

        db = tmp_path / "games.db"
        conn = connect(db)
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO experiments (id, created_at, kind, strategy) VALUES (?,?,?,?)",
            ("exp", 0.0, "live", "farm"),
        )
        conn.executemany(
            "INSERT INTO games (id, experiment_id, started_at, final_score, end_reason) "
            "VALUES (?,?,?,?,?)",
            [(f"old{i}", "exp", 0.0, 100 + i, "game_over") for i in range(DEFAULT_MIN_GAMES)],
        )
        conn.commit()
        conn.close()

        from bot2048.storage.sqlite_recorder import ExperimentMeta, SqliteRecorder

        recorder = SqliteRecorder(db, ExperimentMeta(experiment_id="t", kind="live", strategy="f"))
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1),
            recorder=recorder,
        )
        h.runner.run()
        recorder.close()

        game_over = [n for n in h.notifications if n[1] == "GAME_OVER"]  # type: ignore[attr-defined]
        assert len(game_over) == 1
        # This live game's score beats every seeded 100-ish game -> ~100%.
        assert "минулих farm-ігор" in game_over[0][2]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))

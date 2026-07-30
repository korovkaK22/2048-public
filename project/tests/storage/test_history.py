"""Best-effort historical score percentile for game-end notifications."""

from __future__ import annotations

from pathlib import Path

from bot2048.storage.history import FarmPercentile, farm_score_percentile
from bot2048.storage.schema import connect, ensure_schema


def _seed_games(
    db_path: Path, games: list[tuple[str, int | None, str]]
) -> None:
    """Write ``games`` as (game_id, final_score, end_reason) rows (+ one experiment)."""
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO experiments (id, created_at, kind, strategy) VALUES (?,?,?,?)",
            ("exp", 0.0, "live", "farm"),
        )
        conn.executemany(
            "INSERT INTO games (id, experiment_id, started_at, final_score, end_reason) "
            "VALUES (?,?,?,?,?)",
            [(gid, "exp", 0.0, score, reason) for gid, score, reason in games],
        )
        conn.commit()
    finally:
        conn.close()


class TestFarmScorePercentile:
    def test_ranks_score_among_completed_games(self, tmp_path: Path) -> None:
        db = tmp_path / "games.db"
        _seed_games(
            db,
            [
                ("g1", 10000, "game_over"),
                ("g2", 20000, "game_over"),
                ("g3", 30000, "game_over"),
                ("g4", 40000, "win_2048"),
                ("g5", 50000, "game_over"),
            ],
        )
        result = farm_score_percentile(db, 35000)
        assert result == FarmPercentile(better_pct=60.0, sample_size=5)

    def test_excludes_the_current_game(self, tmp_path: Path) -> None:
        # Six games; excluding the just-ended g6 leaves five in the population.
        db = tmp_path / "games.db"
        _seed_games(
            db,
            [
                ("g1", 10000, "game_over"),
                ("g2", 20000, "game_over"),
                ("g3", 30000, "game_over"),
                ("g4", 40000, "game_over"),
                ("g5", 50000, "game_over"),
                ("g6", 99999, "game_over"),
            ],
        )
        result = farm_score_percentile(db, 99999, exclude_game_id="g6")
        # 99999 beats all 5 remaining games; g6 itself is not counted.
        assert result == FarmPercentile(better_pct=100.0, sample_size=5)

    def test_aborted_and_errored_games_excluded_from_baseline(self, tmp_path: Path) -> None:
        db = tmp_path / "games.db"
        _seed_games(
            db,
            [
                ("g1", 10000, "game_over"),
                ("g2", 20000, "game_over"),
                ("g3", 30000, "game_over"),
                ("g4", 40000, "game_over"),
                ("g5", 50000, "game_over"),
                # Junk scores that must NOT deflate the baseline:
                ("a1", 100, "aborted"),
                ("a2", 200, "interrupted"),
                ("a3", None, "error"),
            ],
        )
        result = farm_score_percentile(db, 25000)
        # Only the 5 completed games count: 2 below 25000 -> 40%.
        assert result is not None
        assert result.sample_size == 5
        assert result.better_pct == 40.0

    def test_too_few_games_returns_none(self, tmp_path: Path) -> None:
        db = tmp_path / "games.db"
        _seed_games(db, [("g1", 10000, "game_over"), ("g2", 20000, "game_over")])
        assert farm_score_percentile(db, 15000, min_games=5) is None

    def test_missing_database_returns_none(self, tmp_path: Path) -> None:
        assert farm_score_percentile(tmp_path / "nope.db", 15000) is None

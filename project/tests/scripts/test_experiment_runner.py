"""Tests for ``scripts/experiment_runner.py`` helpers.

The runner is a standalone script (no package ``__init__``), so it is loaded by
path rather than imported as a module.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "experiment_runner.py"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("experiment_runner", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _load_runner()


def test_run_id_is_collision_resistant_within_one_second(runner: ModuleType) -> None:
    """Two runs of the SAME label/machine/shard at the SAME wall second must
    still get distinct run-folder ids (a random suffix) so ``mkdir`` cannot
    silently overwrite the first run's results."""
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
    ids = {runner._make_run_id("lbl", "windows", 0, 4, now=now) for _ in range(500)}
    assert len(ids) == 500


def test_run_id_encodes_its_parts(runner: ModuleType) -> None:
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
    run_id = runner._make_run_id("sweepA", "m4pro", 1, 8, now=now)
    assert run_id.startswith("sweepA__m4pro__shard1of8__20260711-120000__")
    # trailing suffix is the only variable part
    assert len(run_id.rsplit("__", 1)[1]) == 8


# ==========================================================================
# Aggregates. The objective is P(moves >= 2569) — a TAIL, in MOVES. Before
# these, aggregates.json reported ``moves_mean`` and nothing else about the
# metric the whole project is graded on: no sigma, no fitted tail, no reason
# a losing arm lost.
# ==========================================================================
def _rec(seed: int, moves: int, **extra: Any) -> dict[str, Any]:
    """A per-game record with the pre-telemetry field set (the old format)."""
    return {
        "seed": seed,
        "score": round(moves * 18.2),
        "moves": moves,
        "max_exponent": 10,
        "max_tile": 1024,
        "peak_1024": 1,
        "end_reason": "game_over",
        "forced_2048": False,
        **extra,
    }


class TestMovesStats:
    def test_reports_sigma_quartiles_and_the_fitted_tail(self, runner: ModuleType) -> None:
        moves = [1400, 1500, 1600, 1700, 1800, 1900, 2000, 2100]
        s = runner._moves_stats(moves)
        assert s["moves_mean"] == 1750.0
        assert s["moves_median"] == 1750.0
        assert s["moves_q25"] == 1575.0
        assert s["moves_q75"] == 1925.0
        assert s["moves_max"] == 2100
        assert s["moves_stdev"] == pytest.approx(245.0, abs=1.0)
        # z = (mu - T)/sigma, and P = Phi(z): the ONLY ordering of the objective.
        assert s["moves_z"] == pytest.approx((1750 - 2570) / s["moves_stdev"], abs=0.01)
        lo, hi = s["p_moves_ge_target_ci95"]
        assert lo <= s["p_moves_ge_target"] <= hi

    def test_the_fitted_tail_is_not_the_empirical_one(self, runner: ModuleType) -> None:
        """The target sits ~4.5 sigma out: EVERY affordable run measures 0/n
        empirically, which ranks nothing. The fitted normal tail is what ranks
        arms; both are reported, each honestly labelled."""
        s = runner._moves_stats([1674] * 50 + [1476, 1872] * 25)
        assert s["p_moves_ge_target_empirical"] == 0.0
        assert 0.0 < s["p_moves_ge_target"] < 0.01

    def test_a_wider_arm_can_have_a_fatter_tail_at_a_lower_mean(
        self, runner: ModuleType
    ) -> None:
        """The reason sigma had to be reported at all: ranking by the mean picks
        the WRONG arm (docs/audit-2026-07-11.md §3.4)."""
        narrow = runner._moves_stats([1762 + d for d in (-249, 0, 249) * 20])
        wide = runner._moves_stats([1572 + d for d in (-362, 0, 362) * 20])
        assert wide["moves_mean"] < narrow["moves_mean"]
        assert wide["p_moves_ge_target"] > narrow["p_moves_ge_target"]


class TestAggregate:
    def test_pre_telemetry_records_still_aggregate(self, runner: ModuleType) -> None:
        """MIGRATION: runs recorded before trajectory telemetry existed carry
        none of the new keys. They must still aggregate (so old pools stay
        mergeable) — and the trajectory blocks must be OMITTED, not faked."""
        agg = runner._aggregate([_rec(1000 + i, 1600 + i * 10) for i in range(10)])
        assert agg["games"] == 10
        assert agg["moves_stdev"] > 0
        assert "p_moves_ge_target" in agg
        assert "hazard_100" not in agg
        assert "reservoir_schedule" not in agg
        assert "taboo_blocked_plies_total" not in agg

    def test_hazard_function_comes_straight_out_of_the_trajectories(
        self, runner: ModuleType
    ) -> None:
        """P(death <= 100 | k, e) used to need a one-off instrumentation script.
        Now every run yields it for free."""
        recs = [
            _rec(1, 1600, risk_visits={"4,1": 10}, risk_deaths={"4,1": 8}),
            _rec(2, 1600, risk_visits={"4,1": 10, "2,5": 50}, risk_deaths={"4,1": 7}),
        ]
        agg = runner._aggregate(recs)
        assert agg["hazard_100"]["4,1"] == {"visits": 20, "deaths": 15, "p_death_100": 0.75}
        assert agg["hazard_100"]["2,5"]["p_death_100"] == 0.0

    def test_reservoir_schedule_shows_when_and_how_tight(self, runner: ModuleType) -> None:
        recs = [
            _rec(
                1,
                1900,
                reservoir_births={"1": 491, "2": 957, "3": 1420, "4": 1895},
                birth_crisis={
                    "1": {"min_empty": 3, "mean_empty": 5.0},
                    "4": {"min_empty": 0, "mean_empty": 1.0},
                },
            ),
            _rec(2, 1500, reservoir_births={"1": 501}, birth_crisis={"1": {"min_empty": 2}}),
        ]
        agg = runner._aggregate(recs)
        sched = agg["reservoir_schedule"]
        assert sched["1"]["reached"] == 2
        assert sched["1"]["reached_pct"] == 1.0
        assert sched["1"]["birth_ply_mean"] == 496.0
        assert sched["1"]["birth_min_empty_mean"] == 2.5
        # Only one of the two games ever built a 4th reservoir — and it did so
        # with the board completely starved. That is the failure mode, visible.
        assert sched["4"]["reached_pct"] == 0.5
        assert sched["4"]["birth_min_empty_mean"] == 0.0

    def test_a_mixed_old_new_pool_says_which_games_it_measured(
        self, runner: ModuleType
    ) -> None:
        """The telemetry is Python-side, so a pre-telemetry shard has the SAME
        engine source hash as a new one and ``experiment_merge`` WILL pool them.
        The trajectory blocks then describe a subset of the pool — and must say
        so, and must use that subset as their denominator."""
        recs = [_rec(1, 1600), _rec(2, 1600)]  # old: no telemetry
        recs += [
            _rec(
                3,
                1900,
                risk_visits={"3,2": 10},
                risk_deaths={"3,2": 6},
                reservoir_births={"1": 500},
                birth_crisis={"1": {"min_empty": 2}},
                taboo_blocked_plies=2,
            )
        ]
        agg = runner._aggregate(recs)
        assert agg["games"] == 3
        assert agg["trajectory_games"] == 1
        # reached_pct is out of the games that MEASURED it (1), not the pool (3).
        assert agg["reservoir_schedule"]["1"]["reached_pct"] == 1.0
        # ...and so is the taboo fraction: 2 blocked plies out of that game's 1900.
        assert agg["taboo_blocked_frac"] == pytest.approx(2 / 1900, abs=1e-6)

    def test_end_reasons_and_taboo_pressure_are_reported(self, runner: ModuleType) -> None:
        recs = [
            _rec(1, 1000, taboo_blocked_plies=3),
            _rec(2, 1000, end_reason="tile_cap", forced_2048=True, taboo_blocked_plies=1),
        ]
        agg = runner._aggregate(recs)
        assert agg["end_reason_hist"] == {"game_over": 1, "tile_cap": 1}
        assert agg["end_reason_pct"] == {"game_over": 0.5, "tile_cap": 0.5}
        assert agg["taboo_blocked_plies_total"] == 4
        assert agg["taboo_blocked_frac"] == pytest.approx(4 / 2000)

    def test_summary_md_survives_a_pre_telemetry_pool(
        self, runner: ModuleType, tmp_path: Path
    ) -> None:
        """The merged-pool path renders the same summary; an old pool must not
        crash it, it must just say less."""
        agg = runner._aggregate([_rec(i, 1500 + i) for i in range(5)])
        perf = {
            "machine": "m", "shard": "0/1", "workers": 1, "gomaxprocs": 1,
            "wall_s": 1.0, "games_per_hour": 1.0, "moves_per_sec": 1.0,
            "ms_per_move_mean": None,
        }
        out = tmp_path / "summary.md"
        runner._write_summary_md(
            out, {"label": "l", "depth": 6, "weights": {}}, "rid", agg, perf, 0, 4
        )
        text = out.read_text()
        assert "predates trajectory telemetry" in text
        assert "fitted P(moves >= 2,570)" in text

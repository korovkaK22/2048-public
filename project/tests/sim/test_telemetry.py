"""Tests for ``bot2048.sim.telemetry`` — the per-game trajectory metrics.

These assert the things a post-mortem depends on: that a reservoir's birth ply
is the ply it appeared on, that the hazard counts CENSOR the plies whose fate is
unknown (a game cut off at ``max_moves`` did not die), and that the taboo-safe
choice set really excludes the directions that assemble 2048.
"""

from __future__ import annotations

import random

import pytest

from bot2048.core.types import Board, Direction, SpawnModel
from bot2048.recording import GameEnded, GameStarted, MovePlayed, TileSpawned
from bot2048.sim.runner import play_game
from bot2048.sim.telemetry import (
    HAZARD_HORIZON,
    RANK_1024,
    TelemetryRecorder,
    TrajectoryTracker,
    _choice_set,
    hazard_table,
)

EMPTY: Board = (0,) * 16


def _board(cells: dict[int, int]) -> Board:
    b = [0] * 16
    for i, e in cells.items():
        b[i] = e
    return tuple(b)


# ==========================================================================
# The taboo-aware choice set
# ==========================================================================
class TestChoiceSet:
    def test_two_1024s_with_only_empties_between_make_the_axis_unsafe(self) -> None:
        """The exact taboo law (docs/tail-analysis.md): a direction assembles
        2048 iff some line holds two 1024s with ONLY empty cells between them.
        Here the top row is ``1024 . . 1024`` — L and R both merge it, and they
        die together (LEFT/RIGHT never die apart: §4)."""
        board = _board({0: RANK_1024, 3: RANK_1024, 4: 1, 8: 2})
        legal, safe = _choice_set(board)
        assert legal == 3  # UP does not move anything; L, R, DOWN do
        assert safe == 1  # ...and of those, only DOWN avoids the 2048

    def test_one_separator_of_any_value_saves_the_whole_axis(self) -> None:
        """A single non-empty tile between the two 1024s and nothing is fatal."""
        board = _board({0: RANK_1024, 1: 1, 3: RANK_1024, 4: 1, 8: 2})
        legal, safe = _choice_set(board)
        assert safe == legal == 3

    def test_safe_equals_legal_when_no_1024_pair_can_meet(self) -> None:
        board = _board({0: RANK_1024, 5: 3, 10: 2})
        assert _choice_set(board) == (4, 4)


# ==========================================================================
# Trajectory reduction
# ==========================================================================
def _feed(
    tracker: TrajectoryTracker, plies: list[Board], end_reason: str
) -> TrajectoryTracker:
    """Drive a tracker through a list of DECISION states."""
    for b in plies:
        tracker.on_move(b, int(Direction.LEFT), 0)
        tracker.on_board(b)
    tracker.end_reason = end_reason
    tracker.num_moves = len(plies)
    return tracker


class TestTrajectoryTracker:
    def test_reservoir_birth_is_the_ply_the_tile_appeared_on(self) -> None:
        no1024 = _board({0: 5, 1: 4})
        one = _board({0: RANK_1024, 1: 4})
        two = _board({0: RANK_1024, 2: RANK_1024, 1: 4})
        t = TrajectoryTracker(initial_board=no1024)
        _feed(t, [no1024] * 3 + [one] * 2 + [two] * 4, "game_over")
        # The 1st 1024 is first SEEN on the board of ply 3, the 2nd on ply 5.
        assert t.summary()["reservoir_births"] == {"1": 3, "2": 5}
        assert t.summary()["peak_1024"] == 2

    def test_hazard_counts_deaths_within_the_horizon(self) -> None:
        """A game that DIED: every ply's fate is known, and the plies within
        HAZARD_HORIZON of the end are the deaths."""
        b = _board({0: RANK_1024, 5: 3, 10: 2, 15: 1})  # k=1, e=12
        t = _feed(TrajectoryTracker(initial_board=b), [b] * 250, "game_over")
        s = t.summary()
        key = "1,12"
        assert s["risk_visits"][key] == 250
        assert s["risk_deaths"][key] == HAZARD_HORIZON

    def test_aborted_games_censor_the_last_horizon_plies(self) -> None:
        """A game cut off at ``max_moves`` did NOT die. Counting its final plies
        as survivors would bias the hazard DOWN exactly in the states that
        matter — so they are dropped, not counted."""
        b = _board({0: RANK_1024, 5: 3, 10: 2, 15: 1})
        t = _feed(TrajectoryTracker(initial_board=b), [b] * 250, "aborted")
        s = t.summary()
        assert s["risk_visits"]["1,12"] == 250 - HAZARD_HORIZON
        assert s["risk_deaths"] == {}

    def test_empty_windows_bucket_the_empty_history(self) -> None:
        boards = [_board({0: 1}) for _ in range(120)]  # e = 15
        boards[50] = _board(dict.fromkeys(range(10), 1))  # e = 6
        t = _feed(TrajectoryTracker(initial_board=boards[0]), boards, "game_over")
        w = t.summary()["empty_windows"]
        assert [x["from"] for x in w] == [0, 100]
        assert w[0]["min"] == 6
        assert w[0]["median"] == 15
        assert w[1]["to"] == 120

    def test_birth_crisis_reports_the_squeeze_around_each_birth(self) -> None:
        """The headline of the whole failure mode: the empty-cell minimum in the
        window around a reservoir's birth. Here the board is starved right
        before the 1024 lands."""
        loose = _board({0: 5})  # e = 15
        tight = _board(dict.fromkeys(range(15), 1))  # e = 1
        born = _board({0: RANK_1024, **dict.fromkeys(range(1, 15), 1)})  # e = 1
        boards = [loose] * 50 + [tight] * 10 + [born] * 5
        t = _feed(TrajectoryTracker(initial_board=loose), boards, "game_over")
        crisis = t.summary()["birth_crisis"]
        assert crisis["1"]["min_empty"] == 1
        # BIRTH_WINDOW starts 60 plies before the birth (ply 60), i.e. ply 0 —
        # so the loose run-up is inside the window and drags the mean up.
        assert crisis["1"]["mean_empty"] > 1

    def test_death_block_carries_the_state_20_plies_before_the_end(self) -> None:
        early = _board({0: 5, 1: 4, 2: 3})  # e = 13
        late = _board(dict.fromkeys(range(16), 1))  # e = 0
        boards = [early] * 30 + [late] * 20
        t = _feed(TrajectoryTracker(initial_board=early), boards, "game_over")
        d = t.summary()["death"]
        assert d["reason"] == "game_over"
        assert d["empty_at_minus20"] == 0  # ply 30 = the first `late`
        assert len(d["tail"]) == 20
        assert d["tail"][-1][0] == 49  # last ply index

    def test_taboo_blocked_plies_counts_the_plies_the_taboo_bit(self) -> None:
        safe_board = _board({0: RANK_1024, 1: 1, 3: RANK_1024, 4: 2})
        bitten = _board({0: RANK_1024, 3: RANK_1024, 4: 1, 8: 2})
        t = _feed(
            TrajectoryTracker(initial_board=safe_board),
            [safe_board] * 5 + [bitten] * 2,
            "game_over",
        )
        s = t.summary()
        assert s["taboo_blocked_plies"] == 2
        assert s["legal_dirs_hist"]["3"] == 7  # both boards leave 3 legal moves
        assert s["safe_dirs_hist"] == {"3": 5, "1": 2}  # ...the taboo cuts 2 of them


class TestHazardTable:
    def test_pools_visits_and_deaths_into_a_probability(self) -> None:
        recs = [
            {"risk_visits": {"4,1": 10, "2,5": 100}, "risk_deaths": {"4,1": 6}},
            {"risk_visits": {"4,1": 10}, "risk_deaths": {"4,1": 5, "2,5": 1}},
        ]
        h = hazard_table(recs)
        assert h["4,1"] == {"visits": 20, "deaths": 11, "p_death_100": 0.55}
        assert h["2,5"]["p_death_100"] == 0.01
        # sorted by (k, e), numerically — not lexicographically
        assert list(h) == ["2,5", "4,1"]

    def test_pre_telemetry_records_contribute_nothing_rather_than_lying(self) -> None:
        assert hazard_table([{"seed": 1, "moves": 100, "score": 1}]) == {}


# ==========================================================================
# End-to-end against the real simulator
# ==========================================================================
class _RandomStrategy:
    name = "random"

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def choose(self, board: Board):  # type: ignore[no-untyped-def]
        from bot2048.ai.strategies import Decision, NoLegalMoveError
        from bot2048.core import engine

        legal = engine.legal_moves(board)
        if not legal:
            raise NoLegalMoveError("no legal move")
        return Decision(direction=self._rng.choice(legal), eval_ms=0.0)


class TestEndToEnd:
    @pytest.mark.parametrize("seed", [7, 11])
    def test_a_real_game_produces_a_coherent_trajectory(self, seed: int) -> None:
        rec = TelemetryRecorder(trace=True)
        summary = play_game(
            _RandomStrategy(seed), SpawnModel(), seed=seed, recorder=rec, stop_exponent=11
        )
        tracker = rec.pop(summary.game_id)
        assert tracker is not None
        s = tracker.summary()

        # One decision state per played move, and the windows tile them exactly.
        assert len(tracker.empties) == summary.num_moves
        assert sum(w["to"] - w["from"] for w in s["empty_windows"]) == summary.num_moves
        # The hazard counts can never exceed the plies that produced them.
        assert sum(s["risk_visits"].values()) <= summary.num_moves
        assert sum(s["risk_deaths"].values()) <= sum(s["risk_visits"].values())
        # A game that died: every ply is observed, and the last HORIZON of them
        # are deaths.
        if s["death"]["reason"] == "game_over":
            assert sum(s["risk_visits"].values()) == summary.num_moves
            assert sum(s["risk_deaths"].values()) == min(summary.num_moves, HAZARD_HORIZON)
        assert s["death"]["moves"] == summary.num_moves
        assert len(tracker.trace_rows()) == summary.num_moves
        # A legal move always existed while the game ran.
        assert min(s["legal_dirs_hist"], key=int) != "0" or summary.num_moves == 0

    def test_the_recorder_frees_a_game_when_it_is_popped(self) -> None:
        """A worker plays many games in one process; the per-ply history must
        not accumulate for all of them."""
        rec = TelemetryRecorder()
        summary = play_game(_RandomStrategy(3), SpawnModel(), seed=3, recorder=rec)
        assert rec.pop(summary.game_id) is not None
        assert rec.games == {}
        assert rec.pop(summary.game_id) is None

    def test_it_tees_every_event_to_the_inner_recorder(self) -> None:
        from bot2048.recording import ListRecorder

        inner = ListRecorder()
        rec = TelemetryRecorder(inner)
        play_game(_RandomStrategy(5), SpawnModel(), seed=5, recorder=rec)
        kinds = {type(e).__name__ for e in inner.events}
        assert kinds == {"GameStarted", "MovePlayed", "TileSpawned", "GameEnded"}


class TestRecorderIsEventDriven:
    def test_events_for_an_unknown_game_are_ignored(self) -> None:
        """The recorder is a tee: it must not blow up on events of a game it
        never saw start (e.g. a live game sharing the recorder)."""
        rec = TelemetryRecorder()
        rec.record(
            MovePlayed(
                game_id="ghost",
                ply=0,
                direction=Direction.LEFT,
                board_before=EMPTY,
                board_after=EMPTY,
                score_gained=0,
                score_total=0,
                eval_ms=0.0,
            )
        )
        rec.record(TileSpawned(game_id="ghost", ply=0, spawn=None, board_after=EMPTY))  # type: ignore[arg-type]
        rec.record(
            GameEnded(
                game_id="ghost",
                final_score=0,
                max_exponent=0,
                num_moves=0,
                end_reason="game_over",
                duration_s=0.0,
            )
        )
        assert rec.games == {}

    def test_engine_ms_is_averaged_from_the_eval_details(self) -> None:
        rec = TelemetryRecorder()
        rec.record(
            GameStarted(
                game_id="g",
                experiment_id="e",
                kind="sim",
                strategy="s",
                seed=1,
                initial_board=EMPTY,
            )
        )
        for ms in (10.0, 30.0):
            rec.record(
                MovePlayed(
                    game_id="g",
                    ply=0,
                    direction=Direction.LEFT,
                    board_before=_board({0: 1, 1: 1}),
                    board_after=_board({0: 2}),
                    score_gained=4,
                    score_total=4,
                    eval_ms=ms,
                    eval_details={"engine_ms": ms},
                )
            )
        g = rec.pop("g")
        assert g is not None
        assert g.summary()["engine_ms_mean"] == 20.0

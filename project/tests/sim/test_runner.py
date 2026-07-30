"""Simulator tests: event stream integrity, determinism, reconstructability."""

from __future__ import annotations

import random

from bot2048.ai.strategies import RandomStrategy
from bot2048.core import engine
from bot2048.core.types import EMPTY_BOARD, SpawnModel
from bot2048.recording import (
    GameEnded,
    GameEvent,
    GameStarted,
    ListRecorder,
    MovePlayed,
    TileSpawned,
)
from bot2048.sim.runner import play_game, play_games


def _play(seed: int = 7, max_moves: int | None = 150) -> tuple[list[GameEvent], object]:
    recorder = ListRecorder()
    summary = play_game(
        RandomStrategy(random.Random(seed)),
        SpawnModel(),
        seed,
        recorder,
        max_moves=max_moves,
        experiment_id="exp-test",
        params={"note": "unit"},
    )
    return recorder.events, summary


def test_event_stream_shape() -> None:
    events, _summary = _play()
    assert isinstance(events[0], GameStarted)
    assert isinstance(events[-1], GameEnded)
    started = events[0]
    ended = events[-1]
    assert started.kind == "sim"
    assert started.strategy == "random"
    assert started.experiment_id == "exp-test"
    assert started.seed == 7

    moves = [e for e in events if isinstance(e, MovePlayed)]
    spawns = [e for e in events if isinstance(e, TileSpawned)]
    initial = [s for s in spawns if s.ply < 0]
    assert len(initial) == SpawnModel().initial_tiles
    assert sorted(s.ply for s in initial) == [-2, -1]
    # one spawn per move, plies match
    move_spawns = [s for s in spawns if s.ply >= 0]
    assert [m.ply for m in moves] == list(range(len(moves)))
    assert [s.ply for s in move_spawns] == [m.ply for m in moves]

    assert ended.num_moves == len(moves)
    assert ended.final_score == (moves[-1].score_total if moves else 0)
    assert ended.end_reason in {"game_over", "aborted"}
    assert ended.duration_s > 0


def test_game_is_reconstructable_from_events() -> None:
    """Replaying recorded moves+spawns must reproduce every recorded board."""
    events, _ = _play(seed=11)
    started = events[0]
    assert isinstance(started, GameStarted)

    # rebuild initial board from initial spawns
    cells = list(EMPTY_BOARD)
    for event in events:
        if isinstance(event, TileSpawned) and event.ply < 0:
            cells[event.spawn.cell] = event.spawn.exponent
            assert tuple(cells) == event.board_after
    board = tuple(cells)
    assert board == started.initial_board

    score = 0
    for event in events:
        if isinstance(event, MovePlayed):
            assert event.board_before == board
            result = engine.move(board, event.direction)
            assert result.moved
            assert result.board == event.board_after
            assert result.score_gained == event.score_gained
            score += result.score_gained
            assert score == event.score_total
            board = result.board
        elif isinstance(event, TileSpawned) and event.ply >= 0:
            assert board[event.spawn.cell] == 0
            cells = list(board)
            cells[event.spawn.cell] = event.spawn.exponent
            board = tuple(cells)
            assert board == event.board_after


def test_same_seed_same_game() -> None:
    events_a, summary_a = _play(seed=42)
    events_b, summary_b = _play(seed=42)
    assert summary_a.final_score == summary_b.final_score  # type: ignore[attr-defined]
    assert summary_a.num_moves == summary_b.num_moves  # type: ignore[attr-defined]
    dirs_a = [e.direction for e in events_a if isinstance(e, MovePlayed)]
    dirs_b = [e.direction for e in events_b if isinstance(e, MovePlayed)]
    assert dirs_a == dirs_b


def test_max_moves_aborts() -> None:
    events, _summary = _play(seed=3, max_moves=5)
    ended = events[-1]
    assert isinstance(ended, GameEnded)
    assert ended.num_moves <= 5
    if ended.num_moves == 5:
        assert ended.end_reason == "aborted"


def test_play_games_one_summary_per_seed() -> None:
    recorder = ListRecorder()
    summaries = play_games(
        RandomStrategy(random.Random(0)),
        SpawnModel(),
        seeds=[1, 2, 3],
        recorder=recorder,
        max_moves=30,
    )
    assert len(summaries) == 3
    game_ids = {s.game_id for s in summaries}
    assert len(game_ids) == 3
    started = [e for e in recorder.events if isinstance(e, GameStarted)]
    ended = [e for e in recorder.events if isinstance(e, GameEnded)]
    assert {e.game_id for e in started} == game_ids
    assert {e.game_id for e in ended} == game_ids


def test_stop_exponent_ends_on_capped_tile() -> None:
    """The monobank stop rule: the move that creates a tile at stop_exponent
    ends the game as 'tile_cap' the instant it is made — no spawn is recorded
    after that move, and the final board holds the capped tile.

    stop_exponent=1 caps on ANY tile of value 2, so the very first move always
    triggers it — deterministic regardless of seed or strategy choice."""
    recorder = ListRecorder()
    summary = play_game(
        RandomStrategy(random.Random(4)),
        SpawnModel(),
        seed=4,
        recorder=recorder,
        stop_exponent=1,
        max_moves=200,
    )
    assert summary.end_reason == "tile_cap"
    assert summary.max_exponent >= 1
    assert summary.num_moves == 1  # capped on the first move
    moves = [e for e in recorder.events if isinstance(e, MovePlayed)]
    spawns = [e for e in recorder.events if isinstance(e, TileSpawned)]
    # Exactly the initial tiles are spawned; no post-move spawn shares the
    # capping move's ply (ply 0).
    assert len(moves) == 1
    assert all(s.ply < 0 for s in spawns), "no spawn recorded after the capping move"


def test_no_stop_exponent_never_caps() -> None:
    """Without stop_exponent the runner never emits 'tile_cap' (the cap branch
    is fully opt-in)."""
    events, _summary = _play(seed=11, max_moves=200)
    ended = events[-1]
    assert isinstance(ended, GameEnded)
    assert ended.end_reason in {"game_over", "aborted"}

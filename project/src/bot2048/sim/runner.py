"""Headless game simulator: plays full games and records every event.

``play_game`` is the single source of truth for how a game unfolds:
new_game -> (choose -> move -> spawn)* -> game over / cap / error.
Every played game is fully reconstructable from the emitted events.
"""

from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

from bot2048.ai.strategies import NoLegalMoveError, Strategy
from bot2048.core import engine
from bot2048.core.types import EMPTY_BOARD, SpawnModel
from bot2048.recording import GameEnded, GameStarted, MovePlayed, Recorder, TileSpawned

__all__ = ["GameSummary", "play_game", "play_games"]


@dataclass(frozen=True, slots=True)
class GameSummary:
    """Per-game outcome returned to the caller (also mirrored in GameEnded)."""

    game_id: str
    strategy: str
    seed: int | None
    final_score: int
    max_exponent: int
    end_reason: str  # "game_over" | "aborted" | "tile_cap" | "error"
    num_moves: int
    duration_s: float


def play_game(
    strategy: Strategy,
    spawn_model: SpawnModel,
    seed: int,
    recorder: Recorder,
    max_moves: int | None = None,
    experiment_id: str = "",
    params: dict[str, Any] | None = None,
    stop_exponent: int | None = None,
) -> GameSummary:
    """Play one full game and record every event.

    The game ends with reason ``"game_over"`` when no legal move exists,
    ``"aborted"`` when ``max_moves`` is reached, ``"tile_cap"`` when a move
    creates a tile with exponent >= ``stop_exponent`` (monobank stop rule:
    assembling 2048 ends the game on the spot — no tile is spawned after
    that move), or ``"error"`` if the strategy raises (the exception is
    re-raised after recording).
    """
    rng = random.Random(seed)
    game_id = uuid.uuid4().hex
    started = time.perf_counter()

    board, initial_spawns = engine.new_game(spawn_model, rng)
    recorder.record(
        GameStarted(
            game_id=game_id,
            experiment_id=experiment_id,
            kind="sim",
            strategy=strategy.name,
            seed=seed,
            initial_board=board,
            params=dict(params or {}),
            ts=time.time(),
        )
    )
    # Initial tiles: ply -1, -2, ... in spawn order.
    replay_cells = list(EMPTY_BOARD)
    for i, spawn in enumerate(initial_spawns):
        replay_cells[spawn.cell] = spawn.exponent
        recorder.record(
            TileSpawned(
                game_id=game_id,
                ply=-(i + 1),
                spawn=spawn,
                board_after=tuple(replay_cells),
                ts=time.time(),
            )
        )

    score = 0
    ply = 0
    end_reason = "game_over"
    try:
        while True:
            if engine.is_game_over(board):
                end_reason = "game_over"
                break
            if max_moves is not None and ply >= max_moves:
                end_reason = "aborted"
                break
            try:
                decision = strategy.choose(board)
            except NoLegalMoveError:
                end_reason = "game_over"
                break
            result = engine.move(board, decision.direction)
            if not result.moved:
                raise ValueError(
                    f"strategy {strategy.name!r} chose illegal move "
                    f"{decision.direction.name} at ply {ply}"
                )
            score += result.score_gained
            recorder.record(
                MovePlayed(
                    game_id=game_id,
                    ply=ply,
                    direction=decision.direction,
                    board_before=board,
                    board_after=result.board,
                    score_gained=result.score_gained,
                    score_total=score,
                    eval_ms=decision.eval_ms,
                    search_depth=decision.search_depth,
                    eval_details=decision.details,
                    ts=time.time(),
                )
            )
            if stop_exponent is not None and engine.max_exponent(result.board) >= stop_exponent:
                # Monobank stop rule: the move that assembled the capped tile
                # ends the game immediately — no spawn happens after it.
                board = result.board
                ply += 1
                end_reason = "tile_cap"
                break
            board, spawn = engine.spawn_random(result.board, spawn_model, rng)
            recorder.record(
                TileSpawned(
                    game_id=game_id,
                    ply=ply,
                    spawn=spawn,
                    board_after=board,
                    ts=time.time(),
                )
            )
            ply += 1
    except Exception:
        end_reason = "error"
        raise
    finally:
        duration_s = time.perf_counter() - started
        recorder.record(
            GameEnded(
                game_id=game_id,
                final_score=score,
                max_exponent=engine.max_exponent(board),
                num_moves=ply,
                end_reason=end_reason,
                duration_s=duration_s,
                ts=time.time(),
            )
        )
        recorder.flush()

    return GameSummary(
        game_id=game_id,
        strategy=strategy.name,
        seed=seed,
        final_score=score,
        max_exponent=engine.max_exponent(board),
        end_reason=end_reason,
        num_moves=ply,
        duration_s=duration_s,
    )


def play_games(
    strategy: Strategy,
    spawn_model: SpawnModel,
    seeds: list[int],
    recorder: Recorder,
    max_moves: int | None = None,
    experiment_id: str = "",
    params: dict[str, Any] | None = None,
    stop_exponent: int | None = None,
) -> list[GameSummary]:
    """Play one game per seed and return all summaries."""
    return [
        play_game(
            strategy,
            spawn_model,
            seed,
            recorder,
            max_moves=max_moves,
            experiment_id=experiment_id,
            params=params,
            stop_exponent=stop_exponent,
        )
        for seed in seeds
    ]

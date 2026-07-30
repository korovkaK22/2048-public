"""Tests for the Go engine wrapper, including Go<->Python move parity."""

from __future__ import annotations

import random
import time
from collections.abc import Iterator

import pytest

from bot2048.ai.go_engine import (
    GoEngineError,
    GoExpectimaxStrategy,
    board_to_hex,
    find_engine_binary,
    hex_to_board,
)
from bot2048.ai.strategies import NoLegalMoveError
from bot2048.core import engine
from bot2048.core.types import Board, Direction, SpawnModel
from bot2048.recording import ListRecorder
from bot2048.sim.runner import play_game

pytestmark = pytest.mark.skipif(
    find_engine_binary() is None,
    reason="Go engine binary not built (run tools/engine2048/build.sh)",
)

# A full board with no adjacent equal tiles: no legal move in any direction.
DEAD_BOARD: Board = (1, 2, 1, 2, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)


@pytest.fixture(scope="module")
def strategy() -> Iterator[GoExpectimaxStrategy]:
    with GoExpectimaxStrategy(depth=3) as strat:
        yield strat


@pytest.fixture(scope="module")
def farm_strategy() -> Iterator[GoExpectimaxStrategy]:
    with GoExpectimaxStrategy(depth=4, farm=True) as strat:
        yield strat


def random_board(rng: random.Random) -> Board:
    """Random board: ~40% empty cells, exponents up to 15."""
    return tuple(0 if rng.random() < 0.4 else rng.randint(1, 15) for _ in range(16))


def test_hex_round_trip() -> None:
    board: Board = tuple(range(16))
    assert board_to_hex(board) == "0123456789abcdef"
    assert hex_to_board("0123456789abcdef") == board


def test_move_parity_with_python_engine(strategy: GoExpectimaxStrategy) -> None:
    """Go and Python engines agree on (board, score, moved) for every move."""
    rng = random.Random(0xC0FFEE)
    for _ in range(150):
        board = random_board(rng)
        for direction in Direction:
            expected = engine.move(board, direction)
            got = strategy.move_raw(board, direction)
            assert got == expected, (
                f"parity mismatch: board={board} dir={direction.name} go={got} py={expected}"
            )


def test_choose_returns_legal_move(strategy: GoExpectimaxStrategy) -> None:
    rng = random.Random(7)
    checked = 0
    while checked < 20:
        board = random_board(rng)
        legal = engine.legal_moves(board)
        if not legal:
            continue
        decision = strategy.choose(board)
        assert decision.direction in legal
        assert decision.eval_ms >= 0.0
        assert decision.search_depth is not None and decision.search_depth >= 3
        assert decision.details["engine"] == "go"
        assert "eval" in decision.details and "engine_ms" in decision.details
        checked += 1


def test_choose_raises_on_dead_board(strategy: GoExpectimaxStrategy) -> None:
    assert engine.is_game_over(DEAD_BOARD)
    with pytest.raises(NoLegalMoveError):
        strategy.choose(DEAD_BOARD)


def test_try_lookup_dead_spawn_state_raises_no_legal_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prefetch-cache hit for a dead spawn-state (``bestDir == -1``) must
    raise :class:`NoLegalMoveError`, exactly like the direct ``search`` path --
    NOT a raw ``ValueError`` from ``Direction(-1)``. This exercises only the
    reply-parsing branch, so it is built without a live subprocess."""
    strat = object.__new__(GoExpectimaxStrategy)

    def dead_reply(_cmd: str) -> str:
        return "hit -1 0.0 3"

    monkeypatch.setattr(strat, "_request", dead_reply)
    with pytest.raises(NoLegalMoveError):
        strat._try_lookup("0" * 16)


def test_try_lookup_parses_hit_and_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live spawn-state hit/miss branches stay intact alongside the -1 guard."""
    strat = object.__new__(GoExpectimaxStrategy)

    monkeypatch.setattr(strat, "_request", lambda _cmd: "hit 2 12.5 4")
    assert strat._try_lookup("0" * 16) == (Direction(2), 12.5, 4)

    monkeypatch.setattr(strat, "_request", lambda _cmd: "miss")
    assert strat._try_lookup("0" * 16) is None


def test_name_and_depth() -> None:
    with GoExpectimaxStrategy(depth=4) as strat:
        assert strat.name == "go-expectimax(depth=4)"
        assert strat.depth == 4


def test_rejects_bad_depth() -> None:
    with pytest.raises(ValueError, match="depth"):
        GoExpectimaxStrategy(depth=0)


def test_rejects_bad_budget() -> None:
    with pytest.raises(ValueError, match="budget_ms"):
        GoExpectimaxStrategy(budget_ms=0.0)
    with pytest.raises(ValueError, match="budget_ms"):
        GoExpectimaxStrategy(budget_ms=-5.0)


def test_set_budget_ms_updates_and_rejects_nonpositive() -> None:
    """The mid-game escalation setter: assigns valid values, mirrors the
    constructor validation for invalid ones (brief §5.2 live lever)."""
    strat = GoExpectimaxStrategy(budget_ms=20.0)
    strat.set_budget_ms(800.0)
    assert strat.budget_ms == 800.0
    strat.set_budget_ms(None)
    assert strat.budget_ms is None
    with pytest.raises(ValueError, match="budget_ms"):
        strat.set_budget_ms(0.0)
    with pytest.raises(ValueError, match="budget_ms"):
        strat.set_budget_ms(-5.0)
    # A failed set leaves the current value untouched.
    assert strat.budget_ms is None


def test_budget_mode_name_choose_and_depth_report() -> None:
    """Budget mode: legal move, achieved depth reported, sane wall time."""
    with GoExpectimaxStrategy(budget_ms=20.0) as strat:
        assert strat.name == "go-expectimax(budget=20ms)"
        assert strat.budget_ms == 20.0
        # Mid-game-ish board so several deepening iterations actually run.
        board: Board = (0, 1, 2, 3, 0, 1, 2, 3, 0, 0, 1, 2, 0, 0, 0, 1)
        try:
            decision = strat.choose(board)
        except GoEngineError as exc:  # pragma: no cover - old binary only
            pytest.skip(f"engine binary predates the budget protocol: {exc}")
        assert decision.direction in engine.legal_moves(board)
        # The insurance pass guarantees at least depth 2 is completed.
        assert decision.search_depth is not None and decision.search_depth >= 2
        # Generous bound (machine may be loaded): the hard abort keeps the
        # engine-side time near the budget, far below an unbounded search.
        assert float(decision.details["engine_ms"]) < 500.0


def test_budget_mode_farm_avoids_2048() -> None:
    """The farm objective must survive the budget code path."""
    board: Board = (10, 10, 0, 0, 0, 0, 0, 0, 1, 2, 0, 0, 0, 0, 0, 0)
    with GoExpectimaxStrategy(farm=True, budget_ms=20.0) as strat:
        assert strat.name == "go-expectimax(budget=20ms,farm)"
        try:
            decision = strat.choose(board)
        except GoEngineError as exc:  # pragma: no cover - old binary only
            pytest.skip(f"engine binary predates the budget protocol: {exc}")
    assert not _has_2048(engine.move(board, decision.direction).board)


def test_restarts_after_close(strategy: GoExpectimaxStrategy) -> None:
    board: Board = (1, 1) + (0,) * 14
    first = strategy.choose(board)
    strategy.close()
    second = strategy.choose(board)  # lazily restarts the subprocess
    assert second.direction == first.direction


def _has_2048(board: Board) -> bool:
    return any(exp >= 11 for exp in board)


def test_farm_name_and_flag() -> None:
    with GoExpectimaxStrategy(depth=5, farm=True) as strat:
        assert strat.name == "go-expectimax(depth=5,farm)"
        assert strat.farm is True
    with GoExpectimaxStrategy(depth=5) as strat:
        assert strat.name == "go-expectimax(depth=5)"
        assert strat.farm is False


def test_farm_avoids_2048_when_escape_exists(
    farm_strategy: GoExpectimaxStrategy,
) -> None:
    """Two adjacent 1024s + a free escape: farm must NOT merge them to 2048."""
    # row 0: 1024 1024 . .   (LEFT/RIGHT here would create a 2048 = game end)
    # row 2: 2 4 . .         (UP/DOWN slide these -> a safe, non-terminal move)
    board: Board = (10, 10, 0, 0, 0, 0, 0, 0, 1, 2, 0, 0, 0, 0, 0, 0)
    assert set(engine.legal_moves(board)) == {
        Direction.UP,
        Direction.RIGHT,
        Direction.DOWN,
        Direction.LEFT,
    }
    decision = farm_strategy.choose(board)
    # The chosen move must not produce a 2048 tile.
    result = engine.move(board, decision.direction)
    assert result.moved
    assert not _has_2048(result.board), f"farm chose {decision.direction.name} which creates a 2048"
    assert decision.direction in (Direction.UP, Direction.DOWN)


def test_farm_plays_forced_2048_when_no_alternative(
    farm_strategy: GoExpectimaxStrategy,
) -> None:
    """Full board whose only legal moves merge the two 1024s: farm must still
    return one of them (cash-out) rather than raising or stalling."""
    # Alternating 2/4 checkerboard, except two 1024s adjacent in row 0.
    board: Board = (10, 10, 1, 2, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)
    assert set(engine.legal_moves(board)) == {Direction.LEFT, Direction.RIGHT}
    decision = farm_strategy.choose(board)
    assert decision.direction in (Direction.LEFT, Direction.RIGHT)
    # It is the only kind of move available, so it does create the 2048.
    assert _has_2048(engine.move(board, decision.direction).board)


def test_farm_and_classic_can_diverge(farm_strategy: GoExpectimaxStrategy) -> None:
    """On the escape board, a same-depth classic engine greedily merges up to
    2048 while farm refuses — proving the mode flag changes behaviour."""
    board: Board = (10, 10, 0, 0, 0, 0, 0, 0, 1, 2, 0, 0, 0, 0, 0, 0)
    with GoExpectimaxStrategy(depth=4) as classic_strat:
        classic = engine.move(board, classic_strat.choose(board).direction).board
    farm = engine.move(board, farm_strategy.choose(board).direction).board
    assert _has_2048(classic)  # classic rushes the merge
    assert not _has_2048(farm)  # farm refuses it


def test_plays_full_game_via_runner() -> None:
    recorder = ListRecorder()
    with GoExpectimaxStrategy(depth=2) as strat:
        summary = play_game(
            strategy=strat,
            spawn_model=SpawnModel(),
            seed=42,
            recorder=recorder,
            max_moves=80,
        )
    assert summary.end_reason in ("game_over", "aborted")
    assert summary.num_moves > 0
    assert summary.final_score > 0
    assert recorder.events, "runner must record events"


def test_prefetch_decisions_match_plain_search() -> None:
    """prefetch=True must be a pure latency optimisation: on every board the
    decision (cache hit or miss) is identical to a plain same-depth search."""
    board: Board = (0, 1, 2, 3, 0, 1, 2, 3, 0, 0, 1, 2, 0, 0, 0, 1)
    with (
        GoExpectimaxStrategy(depth=3, prefetch=True) as pre,
        GoExpectimaxStrategy(depth=3) as plain,
    ):
        try:
            first = pre.choose(board)
        except GoEngineError as exc:  # pragma: no cover - old binary only
            pytest.skip(f"engine binary predates the prefetch protocol: {exc}")
        assert first.direction == plain.choose(board).direction
        # The wrapper speculated on the post-swipe board. Wait for the cache
        # DETERMINISTICALLY: poll the engine's read-only `lookup` (it never
        # searches and never aborts the running speculation) until the first
        # spawn state lands, instead of a blind sleep — then walk the 2-spawn
        # states: every decision must match the plain engine whether it was
        # served from the cache or re-searched.
        post_swipe = engine.move(board, first.direction).board
        first_empty = next(i for i, exp in enumerate(post_swipe) if exp == 0)
        first_spawn: Board = (*post_swipe[:first_empty], 1, *post_swipe[first_empty + 1 :])
        deadline = time.monotonic() + 10.0
        while pre._try_lookup(board_to_hex(first_spawn)) is None:
            assert time.monotonic() < deadline, "prefetch cache never filled within 10s"
            time.sleep(0.01)
        hits = 0
        for idx, exp in enumerate(post_swipe):
            if exp != 0:
                continue
            spawn: Board = (*post_swipe[:idx], 1, *post_swipe[idx + 1 :])
            decision = pre.choose(spawn)
            assert decision.direction == plain.choose(spawn).direction
            if decision.details.get("prefetch_hit"):
                hits += 1
        # Each choose() above re-speculates (dropping the previous cache), so
        # only the first spawn state is guaranteed to still be cached — but at
        # least that one must hit after the poll above confirmed it.
        assert hits >= 1, "expected at least one prefetch cache hit"


def test_speculation_gate_skips_in_tight_loop() -> None:
    """Back-to-back choose() calls (sim-bench regime): the gap EMA falls far
    below one spawn-state search cost, so the wrapper must stop speculating
    (and stop probing the guaranteed-miss cache) instead of paying two extra
    round-trips per move against a CPU-saturating speculation worker."""
    board: Board = (0, 1, 2, 3, 0, 1, 2, 3, 0, 0, 1, 2, 0, 0, 0, 1)
    with GoExpectimaxStrategy(budget_ms=20.0, prefetch=True) as strat:
        try:
            first = strat.choose(board)
        except GoEngineError as exc:  # pragma: no cover - old binary only
            pytest.skip(f"engine binary predates the prefetch protocol: {exc}")
        # First call has no pacing information yet -> speculates optimistically.
        assert strat._spec_started is True
        for _ in range(3):
            decision = strat.choose(board)
            assert decision.direction in engine.legal_moves(board)
        # Tight loop: gaps are microseconds, one spawn-state search is ~ the
        # 20 ms budget -> speculation must be gated off.
        assert strat._spec_started is False
        assert first.direction in engine.legal_moves(board)


def test_speculation_gate_stays_on_for_paced_caller() -> None:
    """A live-like caller pauses between moves (animation/perception); the
    gate must keep speculation enabled so prefetch hits stay possible."""
    board: Board = (0, 1, 2, 3, 0, 1, 2, 3, 0, 0, 1, 2, 0, 0, 0, 1)
    with GoExpectimaxStrategy(budget_ms=20.0, prefetch=True) as strat:
        try:
            strat.choose(board)
        except GoEngineError as exc:  # pragma: no cover - old binary only
            pytest.skip(f"engine binary predates the prefetch protocol: {exc}")
        for _ in range(3):
            time.sleep(0.1)  # caller think-time >> one spawn-state search
            decision = strat.choose(board)
            assert decision.direction in engine.legal_moves(board)
        assert strat._spec_started is True

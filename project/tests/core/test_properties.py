"""Hypothesis property tests for the engine's move semantics."""

from __future__ import annotations

from collections import Counter

from hypothesis import given
from hypothesis import strategies as st

from bot2048.core import engine
from bot2048.core.types import Board, Direction

# Exponents 0..11 keep tile values realistic (up to 2048) and leave merge
# headroom well below the 4-bit cell limit.
boards = st.tuples(*[st.integers(min_value=0, max_value=11) for _ in range(16)])
directions = st.sampled_from(list(Direction))


def _mirror_lr(board: Board) -> Board:
    """Mirror the board left<->right."""
    out: list[int] = []
    for r in range(4):
        row = board[r * 4 : r * 4 + 4]
        out.extend(reversed(row))
    return tuple(out)


@given(boards, directions)
def test_tile_multiset_conservation(board: Board, direction: Direction) -> None:
    """Moves only slide and merge: 2 tiles k -> 1 tile k+1, nothing else.

    Invariant: count(k) before == count(k) after + surplus, where every merged
    pair removes two k-tiles and adds one (k+1)-tile. Equivalently, walking
    exponents from high to low, borrow(k) = count_after(k) + 2*borrow(k+1)
    - count_before(k) must be zero for a consistent merge accounting.
    """
    result = engine.move(board, direction)
    before = Counter(e for e in board if e)
    after = Counter(e for e in result.board if e)

    # merges(k) = number of merged pairs of exponent k (creating a k+1 tile)
    max_e = max([*before, *after], default=0)
    merges: dict[int, int] = {}
    score = 0
    for k in range(max_e + 1, 0, -1):
        created = after.get(k, 0) + 2 * merges.get(k, 0) - before.get(k, 0)
        assert created >= 0, f"exponent {k}: tiles appeared from nowhere"
        merges[k - 1] = created  # k tiles created by merging k-1 pairs
        score += created * (2**k)
    assert merges.get(0, 0) == 0, "cannot create tiles by merging empties"
    assert score == result.score_gained


@given(boards)
def test_left_on_mirror_is_mirror_of_right(board: Board) -> None:
    mirrored = _mirror_lr(board)
    left = engine.move(mirrored, Direction.LEFT)
    right = engine.move(board, Direction.RIGHT)
    assert left.board == _mirror_lr(right.board)
    assert left.score_gained == right.score_gained
    assert left.moved == right.moved


@given(boards, directions)
def test_move_is_deterministic(board: Board, direction: Direction) -> None:
    assert engine.move(board, direction) == engine.move(board, direction)


@given(boards, directions)
def test_moved_flag_matches_board_change(board: Board, direction: Direction) -> None:
    result = engine.move(board, direction)
    assert result.moved == (result.board != board)
    if not result.moved:
        assert result.score_gained == 0

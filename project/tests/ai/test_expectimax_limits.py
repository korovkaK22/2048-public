"""Expectimax search limits: golden equality, budget/node caps, adaptive clamp.

The golden fixture ``fixtures/expectimax_depth_golden.json`` was recorded
from the depth-only search BEFORE the limits landed (commit a631b6f, code at
4f61a43): 128 seeded boards (96 random ~40 % empty + 32 cramped with <= 4
empties so the adaptive +1 fires) at depth 2 and 3. Depth-only mode must
keep reproducing it bit for bit. Regenerate (only after an agreed change of
the search itself) from ``project/`` with::

    PYTHONPATH=src python -c \
        "from tests.ai.test_expectimax_limits import write_golden; write_golden()"
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bot2048.ai import expectimax
from bot2048.ai.expectimax import ExpectimaxStrategy
from bot2048.core import engine
from bot2048.core.types import Board, board_from_values

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "expectimax_depth_golden.json"
GOLDEN_SEED = 0x2048
GOLDEN_RANDOM = 96
GOLDEN_CRAMPED = 32
GOLDEN_DEPTHS = (2, 3)
STOP_VOCABULARY = frozenset({"depth", "budget", "nodes", "ladder_end"})

ROOMY = board_from_values((2, 4, 8, 16, 32, 64, 128, 0, 0, 0, 0, 0, 0, 0, 0, 0))
# 3 empty cells <= threshold 4 -> adaptive searches one ply deeper
CRAMPED = board_from_values((2, 4, 2, 4, 4, 2, 4, 2, 2, 4, 2, 0, 4, 2, 0, 0))


# --- golden ----------------------------------------------------------------


def _random_board(rng: random.Random, empty_p: float, max_exp: int) -> Board:
    return tuple(0 if rng.random() < empty_p else rng.randint(1, max_exp) for _ in range(16))


def golden_boards() -> list[Board]:
    """The fixed seeded board set the golden was recorded on."""
    rng = random.Random(GOLDEN_SEED)
    boards: list[Board] = []
    while len(boards) < GOLDEN_RANDOM:
        board = _random_board(rng, 0.4, 11)
        if engine.legal_moves(board):
            boards.append(board)
    while len(boards) < GOLDEN_RANDOM + GOLDEN_CRAMPED:
        board = _random_board(rng, 0.15, 11)
        if engine.legal_moves(board) and len(engine.empty_cells(board)) <= 4:
            boards.append(board)
    return boards


def _record(strategy: ExpectimaxStrategy, boards: list[Board]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for board in boards:
        decision = strategy.choose(board)
        out.append(
            {
                "board": list(board),
                "direction": decision.direction.name,
                "search_depth": decision.search_depth,
                "move_values": decision.details["move_values"],
            }
        )
    return out


def _canonical_sha256(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "sha256"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def write_golden(path: Path = GOLDEN_PATH) -> str:
    """Re-record the golden from the current depth-only search. Returns its sha256."""
    boards = golden_boards()
    payload: dict[str, Any] = {
        "seed": GOLDEN_SEED,
        "n_boards": len(boards),
        "depths": {str(d): _record(ExpectimaxStrategy(depth=d), boards) for d in GOLDEN_DEPTHS},
    }
    payload["sha256"] = _canonical_sha256(payload)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return str(payload["sha256"])


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert payload["sha256"] == _canonical_sha256(payload), "golden fixture was hand-edited"
    assert payload["seed"] == GOLDEN_SEED
    assert payload["n_boards"] == GOLDEN_RANDOM + GOLDEN_CRAMPED
    return payload


def test_golden_board_set_is_reproducible(golden: dict[str, Any]) -> None:
    boards = [list(b) for b in golden_boards()]
    for depth in GOLDEN_DEPTHS:
        assert [rec["board"] for rec in golden["depths"][str(depth)]] == boards


@pytest.mark.parametrize("depth", GOLDEN_DEPTHS)
def test_depth_only_matches_golden(golden: dict[str, Any], depth: int) -> None:
    """Depth-only mode is today's search: same move, same values, same depth."""
    strategy = ExpectimaxStrategy(depth=depth)
    for rec in golden["depths"][str(depth)]:
        decision = strategy.choose(tuple(rec["board"]))
        assert decision.direction.name == rec["direction"], rec["board"]
        assert decision.search_depth == rec["search_depth"], rec["board"]
        assert decision.details["move_values"] == rec["move_values"], rec["board"]
        assert decision.details["search"]["stop"] == "depth"
        assert decision.details["search"]["max_depth"] == rec["search_depth"]


def test_capped_search_that_completes_equals_depth_only(golden: dict[str, Any]) -> None:
    """Direct-first property: budget large enough -> bit-identical to fixed depth."""
    strategy = ExpectimaxStrategy(depth=3, budget_ms=60_000.0, max_nodes=10**9)
    for rec in golden["depths"]["3"][:32]:
        decision = strategy.choose(tuple(rec["board"]))
        assert decision.direction.name == rec["direction"], rec["board"]
        assert decision.search_depth == rec["search_depth"]
        assert decision.details["move_values"] == rec["move_values"], rec["board"]
        assert decision.details["search"]["stop"] == "depth"


# --- limits ------------------------------------------------------------------


def test_depth_only_search_details() -> None:
    decision = ExpectimaxStrategy(depth=2).choose(ROOMY)
    search = decision.details["search"]
    assert search == {
        "stop": "depth",
        "nodes": search["nodes"],
        "max_depth": 2,
        "budget_ms": None,
        "max_nodes": None,
    }
    # at depth 2 the interior chance nodes are exactly the root's legal children
    assert search["nodes"] == len(engine.legal_moves(ROOMY)) == 2


def test_budget_1ms_stops_on_budget_with_completed_depth() -> None:
    decision = ExpectimaxStrategy(depth=None, budget_ms=1.0).choose(ROOMY)
    assert decision.direction in engine.legal_moves(ROOMY)
    assert decision.search_depth is not None and decision.search_depth >= 1
    search = decision.details["search"]
    assert search["stop"] == "budget"
    assert search["budget_ms"] == 1.0
    assert search["max_depth"] is None
    assert decision.details["move_values"]  # from the last COMPLETED depth
    # a 1 ms budget cannot reach depth 4 on a roomy board; overshoot is one poll window
    assert decision.search_depth <= 3
    assert decision.eval_ms < 1000.0


def test_budget_with_depth_cap_never_exceeds_cap() -> None:
    decision = ExpectimaxStrategy(depth=2, budget_ms=60_000.0).choose(ROOMY)
    assert decision.search_depth == 2
    assert decision.details["search"]["stop"] == "depth"


def test_max_nodes_honoured() -> None:
    # ROOMY, cumulative interior nodes through depth d: 0, 2, 119, 2779, ~24k
    cap = 300
    decision = ExpectimaxStrategy(depth=None, max_nodes=cap).choose(ROOMY)
    assert decision.direction in engine.legal_moves(ROOMY)
    search = decision.details["search"]
    assert search["stop"] == "nodes"
    assert search["max_nodes"] == cap
    # the poll fires every _POLL_MASK + 1 nodes: overshoot is bounded by one window
    assert cap <= search["nodes"] <= cap + expectimax._POLL_MASK
    assert decision.search_depth == 3  # depth 3 completed (119), depth 4 aborted

    deeper = ExpectimaxStrategy(depth=None, max_nodes=cap * 10).choose(ROOMY)
    assert deeper.search_depth == 4
    assert deeper.details["search"]["stop"] == "nodes"


def test_depth_one_always_completes_under_tiny_limits() -> None:
    # an already-expired budget: depth 1 still runs, nothing deeper starts
    decision = ExpectimaxStrategy(depth=None, budget_ms=1e-6).choose(ROOMY)
    assert decision.direction in engine.legal_moves(ROOMY)
    assert decision.search_depth == 1
    assert decision.details["search"]["stop"] == "budget"
    # a 1-node cap: depth 1 expands no interior node, depth 2 trips the cap
    for strategy in (
        ExpectimaxStrategy(depth=None, max_nodes=1),
        ExpectimaxStrategy(depth=4, max_nodes=1),
    ):
        decision = strategy.choose(ROOMY)
        assert decision.direction in engine.legal_moves(ROOMY)
        assert decision.search_depth == 2
        assert decision.details["search"]["stop"] == "nodes"
        assert 1 <= decision.details["search"]["nodes"] <= 1 + expectimax._POLL_MASK


def test_unbounded_depth_stops_at_deepening_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(expectimax, "_DEEPENING_CEILING", 2)
    decision = ExpectimaxStrategy(depth=None, budget_ms=60_000.0).choose(ROOMY)
    assert decision.search_depth == 2
    assert decision.details["search"]["stop"] == "ladder_end"
    assert decision.details["search"]["max_depth"] is None


# --- adaptive ----------------------------------------------------------------


def test_adaptive_bonus_is_the_ceiling_and_is_clamped() -> None:
    # today's behaviour: depth 1 + 1 on a cramped board
    assert ExpectimaxStrategy(depth=1).choose(CRAMPED).search_depth == 2
    assert ExpectimaxStrategy(depth=1).choose(ROOMY).search_depth == 1
    # the bonus raises the effective ceiling by exactly one, deepening stops there
    decision = ExpectimaxStrategy(depth=2, budget_ms=60_000.0).choose(CRAMPED)
    assert decision.search_depth == 3
    assert decision.details["search"]["max_depth"] == 3
    assert decision.details["search"]["stop"] == "depth"
    # adaptive off -> ceiling is the configured depth
    plain = ExpectimaxStrategy(depth=2, adaptive=False).choose(CRAMPED)
    assert plain.search_depth == 2
    assert plain.details["search"]["max_depth"] == 2
    # no depth ceiling -> no bonus to apply
    free = ExpectimaxStrategy(depth=None, max_nodes=500).choose(CRAMPED)
    assert free.details["search"]["max_depth"] is None


# --- construction --------------------------------------------------------------


def test_name_keeps_depth_only_format() -> None:
    assert ExpectimaxStrategy(depth=4).name == "expectimax(depth=4)"
    assert ExpectimaxStrategy(depth=3, budget_ms=250.0).name == "expectimax(depth=3,budget=250ms)"
    assert ExpectimaxStrategy(depth=None, max_nodes=50_000).name == "expectimax(nodes=50000)"
    assert ExpectimaxStrategy(depth=None, budget_ms=1.5).name == "expectimax(budget=1.5ms)"


def test_properties() -> None:
    strategy = ExpectimaxStrategy(depth=None, budget_ms=20.0, max_nodes=10)
    assert strategy.depth is None
    assert strategy.budget_ms == 20.0
    assert strategy.max_nodes == 10


@pytest.mark.parametrize(
    "kwargs",
    [
        {"depth": None},
        {"depth": 0},
        {"depth": 3, "budget_ms": 0.0},
        {"depth": 3, "budget_ms": -1.0},
        {"depth": 3, "max_nodes": 0},
    ],
)
def test_rejects_invalid_limits(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ExpectimaxStrategy(**kwargs)


# --- property: the move is always legal ---------------------------------------

_LIMIT_CONFIGS: tuple[dict[str, Any], ...] = (
    {"depth": 2},
    {"depth": None, "max_nodes": 800},
    {"depth": 3, "budget_ms": 1.0},
    {"depth": 2, "max_nodes": 1, "budget_ms": 0.5},
)


@settings(max_examples=60, deadline=None)
@given(
    cells=st.lists(st.integers(min_value=0, max_value=11), min_size=16, max_size=16),
    config_index=st.integers(min_value=0, max_value=len(_LIMIT_CONFIGS) - 1),
)
def test_returned_move_is_always_legal(cells: list[int], config_index: int) -> None:
    board: Board = tuple(cells)
    legal = engine.legal_moves(board)
    if not legal:
        return
    config = _LIMIT_CONFIGS[config_index]
    decision = ExpectimaxStrategy(**config).choose(board)
    assert decision.direction in legal
    assert decision.search_depth is not None and decision.search_depth >= 1
    search = decision.details["search"]
    assert search["stop"] in STOP_VOCABULARY
    assert search["nodes"] >= 0
    if config.get("max_nodes") is not None:
        assert search["nodes"] <= config["max_nodes"] + expectimax._POLL_MASK
    if config.get("depth") is not None:
        assert decision.search_depth <= config["depth"] + 1

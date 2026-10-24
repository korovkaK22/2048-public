"""Protocol-2 search limits in the Go wrapper (S2b).

The parsing / wire-shape tests run against a scripted engine (no subprocess);
the integration tests need the built ``engine2048`` binary and skip without it,
like :mod:`tests.ai.test_go_engine`. Env isolation and engine identity live in
:mod:`tests.ai.test_go_engine_identity`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from bot2048.ai import go_engine
from bot2048.ai.go_engine import (
    GoEngineError,
    GoExpectimaxStrategy,
    SearchLimits,
    find_engine_binary,
)
from bot2048.core import engine
from bot2048.core.types import Board

needs_engine = pytest.mark.skipif(
    find_engine_binary() is None,
    reason="Go engine binary not built (run tools/engine2048/build.sh)",
)

MIDGAME: Board = (0, 1, 2, 3, 0, 1, 2, 3, 0, 0, 1, 2, 0, 0, 0, 1)


# --- scripted engine (no subprocess) ----------------------------------------


def scripted(
    monkeypatch: pytest.MonkeyPatch,
    protocol: int,
    reply: str | Callable[[str], str],
    **kwargs: object,
) -> tuple[GoExpectimaxStrategy, list[str]]:
    """A strategy whose engine is a script: records request lines, answers ``reply``.

    No binary is resolved (the resolver is stubbed) and no process is spawned
    (``_ensure_proc`` only pins the protocol version the handshake would have).
    """
    monkeypatch.setattr(go_engine, "resolve_engine_binary", lambda _b: Path("stub-engine"))
    strat = GoExpectimaxStrategy(**kwargs)  # type: ignore[arg-type]
    sent: list[str] = []

    def fake_ensure() -> None:
        strat._protocol = protocol
        strat._engine_info = {"protocol": str(protocol), "BOT2048_SEARCH_WORKERS": "4"}

    def fake_request(line: str) -> str:
        sent.append(line)
        return reply(line) if callable(reply) else reply

    monkeypatch.setattr(strat, "_ensure_proc", fake_ensure)
    monkeypatch.setattr(strat, "_request", fake_request)
    return strat, sent


def test_split_reply_tail() -> None:
    assert go_engine._split_reply_tail("2 12.5 3.1 4") == (["2", "12.5", "3.1", "4"], {})
    fields, tail = go_engine._split_reply_tail("2 12.5 3.1 4 stop=budget nodes=1234 cprob=1e-05")
    assert fields == ["2", "12.5", "3.1", "4"]
    assert tail == {"stop": "budget", "nodes": "1234", "cprob": "1e-05"}


def test_v2_reply_tail_lands_in_search_details(monkeypatch: pytest.MonkeyPatch) -> None:
    strat, sent = scripted(
        monkeypatch,
        2,
        "3 1596832.713092 2.5 4 stop=depth nodes=4527 cprob=0.0001",
        limits=SearchLimits(max_depth=4, budget_ms=250.0),
    )
    decision = strat.choose(MIDGAME)
    assert decision.search_depth == 4
    assert decision.details["search"] == {
        "stop": "depth",
        "nodes": 4527,
        "cprob": 0.0001,
        "max_depth": 4,
        "budget_ms": 250.0,
        "max_nodes": None,
        "workers": 4,
    }
    # The v2 wire: six positional fields + the limits tail (budget 0 = none).
    assert sent == [f"search {go_engine.board_to_hex(MIDGAME)} 4 0.1 1 0 250.0 maxdepth=4 nodes=0"]


def test_v2_wire_for_each_limits_row(monkeypatch: pytest.MonkeyPatch) -> None:
    hex_board = go_engine.board_to_hex(MIDGAME)
    cases: list[tuple[dict[str, object], str]] = [
        ({"depth": 6}, f"search {hex_board} 6 0.1 1 0 0.0 maxdepth=6 nodes=0"),
        ({"budget_ms": 250.0}, f"search {hex_board} 6 0.1 1 0 250.0 maxdepth=0 nodes=0"),
        (
            {"limits": SearchLimits(max_depth=None, budget_ms=None, max_nodes=50_000_000)},
            f"search {hex_board} 6 0.1 1 0 0.0 maxdepth=0 nodes=50000000",
        ),
        (
            {"farm": True, "cprob": 1e-5, "limits": SearchLimits(max_depth=4, max_nodes=10)},
            f"search {hex_board} 4 0.1 1 1 0.0 maxdepth=4 nodes=10 cprob=1e-05",
        ),
    ]
    for kwargs, want in cases:
        strat, sent = scripted(
            monkeypatch, 2, "3 1.0 0.1 4 stop=depth nodes=1 cprob=0.0001", **kwargs
        )
        strat.choose(MIDGAME)
        assert sent == [want], kwargs


def test_protocol1_fallback_infers_stop_and_keeps_old_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """A protocol-1 binary sends no tail: stop is inferred from the mode, the
    other telemetry is None, and the request is the old positional wire."""
    hex_board = go_engine.board_to_hex(MIDGAME)
    strat, sent = scripted(monkeypatch, 1, "3 1.0 0.1 3", depth=3)
    details = strat.choose(MIDGAME).details["search"]
    assert details["stop"] == "depth"
    assert details["nodes"] is None and details["cprob"] is None
    assert details["max_depth"] == 3 and details["budget_ms"] is None
    assert sent == [f"search {hex_board} 3 0.1 1"]

    strat, sent = scripted(monkeypatch, 1, "3 1.0 0.1 5", budget_ms=20.0, farm=True)
    assert strat.choose(MIDGAME).details["search"]["stop"] == "budget"
    assert sent == [f"search {hex_board} 6 0.1 1 1 20.0"]


def test_protocol1_refuses_limits_it_cannot_express(monkeypatch: pytest.MonkeyPatch) -> None:
    for kwargs in (
        {"limits": SearchLimits(max_depth=4, budget_ms=250.0)},
        {"limits": SearchLimits(max_depth=4, max_nodes=1000)},
        {"limits": SearchLimits()},
        {"depth": 4, "cprob": 1e-5},
    ):
        strat, sent = scripted(monkeypatch, 1, "3 1.0 0.1 4", **kwargs)
        with pytest.raises(GoEngineError, match="protocol 1"):
            strat.choose(MIDGAME)
        assert sent == [], "nothing may reach a protocol-1 engine it would silently degrade"


def test_set_limits_resets_prefetch_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ladder step must not let a lookup serve a decision computed under the
    previous limits: the speculation gate is cleared on every set_limits."""
    strat, _ = scripted(monkeypatch, 2, "miss", budget_ms=250.0, prefetch=True)
    strat._spec_started = True
    strat.set_limits(SearchLimits(max_depth=6, budget_ms=2000.0))
    assert strat._spec_started is False
    assert strat.limits == SearchLimits(max_depth=6, budget_ms=2000.0)
    strat._spec_started = True
    strat.set_budget_ms(9000.0)
    assert strat._spec_started is False
    # Legacy alias semantics: in budget mode the cap is inherited by the new budget.
    assert strat.limits == SearchLimits(max_depth=6, budget_ms=9000.0)


def test_set_budget_ms_legacy_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    strat, _ = scripted(monkeypatch, 2, "miss", depth=4)
    assert strat.limits == SearchLimits(max_depth=4)
    strat.set_budget_ms(250.0)  # fixed -> budget lifts the cap (time-only, as today)
    assert strat.limits == SearchLimits(max_depth=None, budget_ms=250.0)
    strat.set_budget_ms(None)  # back to fixed depth, never to unbounded by accident
    assert strat.limits == SearchLimits(max_depth=4)
    with pytest.raises(TypeError):
        strat.set_limits(250.0)  # type: ignore[arg-type]


def test_limits_validation() -> None:
    with pytest.raises(ValueError, match="max_depth"):
        SearchLimits(max_depth=0)
    with pytest.raises(ValueError, match="budget_ms"):
        SearchLimits(budget_ms=0.0)
    with pytest.raises(ValueError, match="max_nodes"):
        SearchLimits(max_nodes=0)
    with pytest.raises(ValueError, match="not both"):
        GoExpectimaxStrategy(budget_ms=1.0, limits=SearchLimits())


def test_reply_timeout_tracks_hang_timeout_and_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    strat, _ = scripted(monkeypatch, 2, "miss", depth=4, hang_timeout_s=60.0)
    assert strat._reply_timeout_s() == 60.0
    strat.set_limits(SearchLimits(budget_ms=250.0))
    assert strat._reply_timeout_s() == 60.0  # 8 x 0.25 s is below the floor
    strat.set_limits(SearchLimits(budget_ms=9000.0))
    assert strat._reply_timeout_s() == 72.0  # 8 x 9 s
    strat.set_limits(SearchLimits(max_nodes=10))  # unbounded: the watchdog alone
    assert strat._reply_timeout_s() == 60.0


# --- integration (built engine2048) ------------------------------------------


@needs_engine
def test_version_reports_protocol_2() -> None:
    assert int(go_engine.engine_version()["protocol"]) >= 2


@needs_engine
def test_strategy_handshake_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT2048_CPROB_THRESH", "0.5")
    with GoExpectimaxStrategy(depth=2, env={}) as strat:
        strat.choose(MIDGAME)
        assert strat.protocol >= 2
        assert strat.engine_info["engine"] == "engine2048"
        assert strat.engine_info["BOT2048_CPROB_THRESH"] == "0.0001"


@needs_engine
def test_capped_and_completed_matches_fixed_depth() -> None:
    """The owner's property, end to end: (max_depth=D, budget) that completes
    == fixed depth D, bit-for-bit, and says stop=depth."""
    with (
        GoExpectimaxStrategy(limits=SearchLimits(max_depth=3, budget_ms=10_000.0)) as capped,
        GoExpectimaxStrategy(depth=3) as fixed,
    ):
        for board in (MIDGAME, (1, 1, 2, 3, 0, 5, 2, 3, 0, 0, 1, 2, 4, 0, 0, 1)):
            a = capped.choose(board)
            b = fixed.choose(board)
            assert (a.direction, a.details["eval"], a.search_depth) == (
                b.direction,
                b.details["eval"],
                b.search_depth,
            )
            assert a.details["search"]["stop"] == "depth"
            assert b.details["search"]["stop"] == "depth"
            assert a.details["search"]["nodes"] == b.details["search"]["nodes"] > 0


@needs_engine
def test_node_cap_and_tiny_budget_stop_reasons() -> None:
    with GoExpectimaxStrategy(limits=SearchLimits(max_depth=6, max_nodes=1000)) as strat:
        decision = strat.choose(MIDGAME)
        assert decision.direction in engine.legal_moves(MIDGAME)
        assert decision.details["search"]["stop"] == "nodes"
    with GoExpectimaxStrategy(limits=SearchLimits(max_depth=8, budget_ms=0.5)) as strat:
        decision = strat.choose(MIDGAME)
        assert decision.direction in engine.legal_moves(MIDGAME)
        assert decision.details["search"]["stop"] in ("budget", "depth")
        assert decision.search_depth is not None and decision.search_depth <= 8
    with GoExpectimaxStrategy(limits=SearchLimits(max_nodes=20_000)) as strat:  # unbounded + guard
        decision = strat.choose(MIDGAME)
        assert decision.details["search"]["stop"] in ("nodes", "ladder_end")

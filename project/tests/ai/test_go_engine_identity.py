"""Engine process environment and engine identity in the Go wrapper (S2).

``env=`` must be the ONLY source of ``BOT2048_*``/``GOMAXPROCS`` the engine
sees when given; ``label=`` names the strategy; ``engine_version`` hashes the
sources of the engine that actually answered (``engine=`` key), so an ntuple run
never records engine2048's hash. The subprocess-free tests always run; the
integration tests need the built ``engine2048`` binary and skip without it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot2048.ai import go_engine
from bot2048.ai.go_engine import (
    GoExpectimaxStrategy,
    engine_kind,
    engine_source_sha256,
    engine_version,
    find_engine_binary,
)

needs_engine = pytest.mark.skipif(
    find_engine_binary() is None,
    reason="Go engine binary not built (run tools/engine2048/build.sh)",
)


def test_engine_env_strips_engine_keys_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT2048_FAKE_LEAK", "1")
    monkeypatch.setenv("GOMAXPROCS", "3")
    monkeypatch.setenv("BOT2048_KEEP_ME", "shell")
    assert go_engine._engine_env(None) is None  # inherit, as before
    clean = go_engine._engine_env({"BOT2048_KEEP_ME": "spec", "BOT2048_NEW": "x"})
    assert clean is not None
    assert "BOT2048_FAKE_LEAK" not in clean and "GOMAXPROCS" not in clean
    assert clean["BOT2048_KEEP_ME"] == "spec" and clean["BOT2048_NEW"] == "x"
    assert clean.get("PATH") == os.environ.get("PATH")  # the rest still reaches the engine


def test_popen_receives_clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine process gets exactly the cleaned env (a fake BOT2048_* in
    os.environ never reaches it when env= is given); env=None inherits."""
    monkeypatch.setenv("BOT2048_FAKE_LEAK", "1")
    monkeypatch.setattr(go_engine, "resolve_engine_binary", lambda _b: Path("stub-engine"))
    seen: list[dict[str, str] | None] = []

    class BoomPopen:
        def __init__(self, *_a: object, env: dict[str, str] | None = None, **_k: object) -> None:
            seen.append(env)
            raise OSError("stub: no process")

    monkeypatch.setattr(go_engine.subprocess, "Popen", BoomPopen)
    with pytest.raises(OSError):
        GoExpectimaxStrategy(depth=2, env={"BOT2048_CPROB_THRESH": "0.01"})._ensure_proc()
    with pytest.raises(OSError):
        GoExpectimaxStrategy(depth=2)._ensure_proc()
    assert len(seen) == 2
    isolated, inherited = seen
    assert isolated is not None and "BOT2048_FAKE_LEAK" not in isolated
    assert isolated["BOT2048_CPROB_THRESH"] == "0.01"
    assert inherited is None


def test_label_overrides_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(go_engine, "resolve_engine_binary", lambda _b: Path("stub-engine"))
    strat = GoExpectimaxStrategy(budget_ms=250.0, farm=True, label="champion-live@ab12cd34")
    assert strat.name == "champion-live@ab12cd34"
    plain = GoExpectimaxStrategy(budget_ms=250.0, farm=True)
    assert plain.name == "go-expectimax(budget=250ms,farm)"


def test_engine_kind_from_reply_then_path() -> None:
    assert engine_kind({"engine": "ntuple"}) == "ntuple"
    assert engine_kind({"engine": "engine2048"}) == "engine2048"
    assert engine_kind({}, Path("tools/ntuple/ntuple.exe")) == "ntuple"
    assert engine_kind({"binary": "tools/ntuple/ntuple"}) == "ntuple"
    assert engine_kind({}, Path("somewhere/else/bin.exe")) == "engine2048"


def test_source_sha256_is_per_engine() -> None:
    e = engine_source_sha256(go_engine._tools_dir("engine2048"))
    n = engine_source_sha256(go_engine._tools_dir("ntuple"))
    assert e == engine_source_sha256()  # default = engine2048, as before
    assert e != n
    with pytest.raises(ValueError, match="engine kind"):
        go_engine._tools_dir("other")


@needs_engine
def test_engine_version_env_isolation_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine reports the cprob it ACTUALLY applied: a shell export is
    honoured only when env is inherited (None) and stripped when env is given."""
    monkeypatch.setenv("BOT2048_CPROB_THRESH", "0.5")
    inherited = engine_version()
    isolated = engine_version(env={})
    pinned = engine_version(env={"BOT2048_CPROB_THRESH": "0.01"})
    assert inherited["BOT2048_CPROB_THRESH"] == "0.5"
    assert isolated["BOT2048_CPROB_THRESH"] == "0.0001"
    assert pinned["BOT2048_CPROB_THRESH"] == "0.01"
    assert inherited["engine"] == "engine2048"
    assert inherited["source_sha256"] == engine_source_sha256(go_engine._tools_dir("engine2048"))

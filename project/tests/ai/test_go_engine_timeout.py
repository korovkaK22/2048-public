"""HANG-PREVENTION #2: a STALLED Go engine can no longer freeze ``choose()``.

Engine DEATH was already handled (empty readline -> ``GoEngineError``, the loop
respawns), but a STALL -- a hung iterative-deepening loop or a wedged pipe --
left ``_request`` blocked on a bare ``readline()`` forever, and a blocked thread
never raises, so the never-crash loop could not recover. A daemon reader drains
stdout into a queue and ``_request`` waits with ``queue.get(timeout=...)``; on
expiry it kills the process and raises the SAME ``GoEngineError`` the death path
raises (recoverable).

Proven per fix: (a) a genuine stall (no reply) -> ``GoEngineError`` + the process
is killed for respawn; (b) a reply arriving UNDER the bound returns normally and
leaves the process alive; and the bound itself sits far above the worst legit
search so it can never fire spuriously.

Hardware-free: ``subprocess.Popen`` is replaced by a fake proc whose stdout is
controllable. No real Go engine binary is required.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from bot2048.ai import go_engine
from bot2048.ai.go_engine import GoEngineError, GoExpectimaxStrategy, SearchLimits


class _FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, data: str) -> int:
        self.lines.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStdout:
    """One optional reply, then blocks until the proc is killed/closed (EOF).

    ``reply=None`` models a STALL (the engine accepted the request but never
    answers). A non-None reply is served once after ``reply_delay`` seconds,
    modelling a slow-but-legitimate search.
    """

    def __init__(self, reply: str | None, reply_delay: float) -> None:
        self._reply = reply
        self._delay = reply_delay
        self._served = False
        self._released = threading.Event()

    def readline(self) -> str:
        if self._reply is not None and not self._served:
            self._served = True
            if self._delay:
                time.sleep(self._delay)
            return self._reply
        self._released.wait()  # park until kill()/close() -> then EOF
        return ""

    def release(self) -> None:
        self._released.set()

    def close(self) -> None:
        self.release()


class _FakeProc:
    def __init__(self, reply: str | None = None, reply_delay: float = 0.0) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(reply, reply_delay)
        self._alive = True
        self.killed = False

    def poll(self) -> int | None:
        return None if self._alive else 0

    def kill(self) -> None:
        self.killed = True
        self._alive = False
        self.stdout.release()  # unblock the parked reader -> thread exits

    def wait(self, timeout: float | None = None) -> int:
        self._alive = False
        return 0


def _bare_strategy() -> GoExpectimaxStrategy:
    """A strategy skeleton with just the fields ``_request`` touches (no binary)."""
    strat = object.__new__(GoExpectimaxStrategy)
    strat._proc = None
    strat._reply_q = None
    strat._reader = None
    strat._limits = SearchLimits(max_depth=6)
    strat._hang_timeout_s = go_engine._REPLY_TIMEOUT_FLOOR_S
    strat._env = None
    strat._binary = Path("engine")  # never launched: Popen is monkeypatched
    # The per-spawn `version` handshake is a real round-trip in production; the
    # fake proc here answers one scripted line only, so pin its outcome instead.
    strat._handshake = lambda _proc, _q: None  # type: ignore[method-assign]
    strat._protocol = 1
    strat._engine_info = {}
    return strat


def _patch_popen(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> None:
    monkeypatch.setattr(go_engine.subprocess, "Popen", lambda *a, **k: proc)


# -- (a) a genuine stall -> recoverable GoEngineError + process killed --------


def test_stall_raises_go_engine_error_and_kills_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc(reply=None)  # never answers
    _patch_popen(monkeypatch, proc)
    strat = _bare_strategy()
    monkeypatch.setattr(strat, "_reply_timeout_s", lambda: 0.15)  # inject small bound

    with pytest.raises(GoEngineError, match="stalled"):
        strat._request("search 0000000000000000 6 0.1 1")

    assert proc.killed is True  # hung engine was killed...
    assert strat._proc is None  # ...and cleared so the next choose() respawns


# -- (b) a reply UNDER the bound returns normally; process stays alive --------


def test_reply_under_bound_succeeds_without_firing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc(reply="2 12.5 8 4\n", reply_delay=0.05)
    _patch_popen(monkeypatch, proc)
    strat = _bare_strategy()
    monkeypatch.setattr(strat, "_reply_timeout_s", lambda: 0.5)  # reply arrives first

    reply = strat._request("search 0000000000000000 6 0.1 1")

    assert reply == "2 12.5 8 4"  # stripped reply returned unchanged
    assert proc.killed is False  # watchdog did NOT fire
    assert strat._proc is proc  # process kept warm for the next move
    proc.kill()  # release the parked reader for a clean test teardown


# -- the bound itself cannot fire spuriously ---------------------------------


def test_reply_timeout_sits_above_endgame_budget() -> None:
    # Fixed-depth: the 15 s floor, comfortably above a deep search and the
    # <=9 s live endgame budget.
    fixed = _bare_strategy()
    assert fixed._reply_timeout_s() == 15.0
    assert fixed._reply_timeout_s() > 9.0

    # Budget mode: 8x the budget, never below the floor. Even a 9 s endgame
    # budget yields a 72 s wait -- only a true stall waits that long.
    small = _bare_strategy()
    small._limits = SearchLimits(budget_ms=800.0)
    assert small._reply_timeout_s() == 15.0  # floor wins for small budgets
    big = _bare_strategy()
    big._limits = SearchLimits(budget_ms=9000.0)
    assert big._reply_timeout_s() == pytest.approx(72.0)
    assert big._reply_timeout_s() > 9000.0 / 1000.0

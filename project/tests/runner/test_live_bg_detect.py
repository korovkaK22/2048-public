"""Live loop with the ASYNC control-tick worker (:mod:`bot2048.runner.bg_detect`).

The periodic full-frame ``detect_state`` that catches popups / auth the 22-byte
packet cannot see now runs OFF the play loop. These tests pin the four
guarantees the move to a background thread must preserve:

* (a) the play loop keeps committing moves while a real background worker is
  stuck in a slow ~0.65 s ``screencap`` -- it is never blocked by the tick;
* (b) a background game-over detection is handled fail-closed: the loop
  re-confirms on the main thread and finalizes the game;
* (c) a silent / dead worker is never WORSE than before -- the packet's own
  not-in-game flag stays an immediate in-loop hint, and a dead worker falls
  back to the synchronous move-based tick, which still catches the popup;
* (d) the worker is started and cleanly closed around the session.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot2048.ai.strategies import Decision
from bot2048.core import engine
from bot2048.core.types import Board, Direction, Spawn
from bot2048.recording import GameEnded, MovePlayed
from bot2048.runner.escalation import ProbeResult
from bot2048.runner.live import LiveConfig
from bot2048.vision.states import ScreenState
from tests.runner.conftest import FakePhone, ScriptedStrategy, make_harness
from tests.runner.test_live_apk import FakeBoardSource

START_BOARD: Board = (1, 1) + (0,) * 14
#: Two well-separated tiles: a handful of alternating slides all stay legal.
OPEN_BOARD: Board = (1,) + (0,) * 14 + (1,)

_GAMEOVER_PROBE = ProbeResult(
    ScreenState.GAME_OVER_POPUP, None, None, 0.0, "bg saw «Гру завершено»"
)


@dataclass
class FakeBgDetect:
    """Scripted :class:`~bot2048.runner.bg_detect.StateProbe` (no real thread).

    ``alerts`` are handed out one per :meth:`poll_alert`; when empty the worker
    is "silent" (returns ``None``). ``running`` toggles the loop between the
    async consume path and the synchronous fallback tick.
    """

    alerts: deque[ProbeResult] = field(default_factory=deque)
    running: bool = True
    started: bool = False
    closed: bool = False
    poll_calls: int = 0
    active_calls: list[bool] = field(default_factory=list)

    def start(self) -> None:
        self.started = True

    def poll_alert(self) -> ProbeResult | None:
        self.poll_calls += 1
        return self.alerts.popleft() if self.alerts else None

    def note_active(self, active: bool) -> None:
        self.active_calls.append(active)

    @property
    def is_running(self) -> bool:
        return self.running

    def close(self) -> None:
        self.closed = True


class FirstLegalStrategy:
    """Plays the first direction that actually moves (keeps the game legal)."""

    name = "first-legal"

    def choose(self, board: Board) -> Decision:
        for direction in Direction:
            if engine.move(board, direction).moved:
                return Decision(direction=direction, eval_ms=0.1)
        raise AssertionError("no legal move on a non-terminal board")


def _events(recorder: object, kind: type) -> list[object]:
    return [e for e in recorder.events if isinstance(e, kind)]  # type: ignore[attr-defined]


# -- (b) a background alert is handled fail-closed on the main thread ----------


class TestBackgroundAlertFinalizes:
    def test_bg_gameover_alert_finalizes_via_main_thread_confirm(
        self, tmp_path: Path
    ) -> None:
        # Packets stay healthy, so ONLY the background worker sees the popup the
        # app raised after move 1. Its alert is consumed at the top of move 2;
        # the loop re-confirms with a full frame (which sees «Гру завершено»)
        # and finalizes game_over -- move 2 never swipes blindly.
        phone = FakePhone(
            board=START_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            game_over_after_swipes=1,
        )
        source = FakeBoardSource(phone)  # always-healthy packets
        bg = FakeBgDetect(alerts=deque([_GAMEOVER_PROBE]))
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.RIGHT]),
            config=LiveConfig(
                experiment_id="t",
                max_games=1,
                settle_stable_frames=1,
                apk_detect_every_moves=1,
            ),
            board_source=source,
            bg_detect=bg,
        )
        h.runner.run()

        assert len(_events(h.recorder, MovePlayed)) == 1
        ended = _events(h.recorder, GameEnded)
        assert [e.end_reason for e in ended] == ["game_over"]  # type: ignore[attr-defined]
        assert phone.swipes == [Direction.RIGHT]  # move 2 never swiped
        assert bg.started and bg.closed  # started + cleanly closed (d)
        assert bg.poll_calls >= 1  # the async result drove the tick


# -- (c) silent / dead worker is never worse than before -----------------------


class TestSilentWorkerNotWorse:
    def test_running_but_silent_worker_relies_on_packet_flag(
        self, tmp_path: Path
    ) -> None:
        # The worker never alerts (silent). The fatal swipe raises the popup and
        # the sampler flags NOT_IN_GAME: the packet path pulls a full frame and
        # finalizes game_over immediately -- no blind tap, no dependence on the
        # background at all.
        phone = FakePhone(
            board=START_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            game_over_after_swipes=1,
        )
        source = FakeBoardSource(phone, script=deque(["not_in_game"]))
        bg = FakeBgDetect()  # running, but never produces an alert
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(
                experiment_id="t", max_games=1, apk_detect_every_moves=1
            ),
            board_source=source,
            bg_detect=bg,
        )
        h.runner.run()

        assert _events(h.recorder, MovePlayed) == []  # popup hid the after-board
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == [  # type: ignore[attr-defined]
            "game_over"
        ]
        assert source.polls == 1  # packet flag -> immediate confirm
        assert phone.taps == []  # no blind tap

    def test_dead_worker_falls_back_to_synchronous_tick(self, tmp_path: Path) -> None:
        # The worker is not running (never came up / died). The loop must revert
        # to the synchronous move-based tick, which still catches the popup at
        # the top of move 2 -- never worse than the pre-async behavior.
        phone = FakePhone(
            board=START_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            game_over_after_swipes=1,
        )
        source = FakeBoardSource(phone)  # healthy packets
        bg = FakeBgDetect(running=False)  # dead worker
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.RIGHT]),
            config=LiveConfig(
                experiment_id="t",
                max_games=1,
                settle_stable_frames=1,
                apk_detect_every_moves=1,
            ),
            board_source=source,
            bg_detect=bg,
        )
        h.runner.run()

        assert len(_events(h.recorder, MovePlayed)) == 1
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == [  # type: ignore[attr-defined]
            "game_over"
        ]
        assert phone.swipes == [Direction.RIGHT]  # sync fallback caught the popup
        assert bg.poll_calls == 0  # dead worker was never polled


# -- (a) the play loop is not blocked by a slow background screencap -----------


class TestLoopNotBlockedBySlowDetect:
    def test_moves_commit_while_real_worker_is_stuck_in_slow_screencap(
        self, tmp_path: Path
    ) -> None:
        # A REAL background worker whose every screencap costs 0.3 s. With the
        # OLD synchronous tick (cadence 1) the loop would screencap once per
        # move (>=6 blocking captures for 5 moves). Async, the loop reads its
        # moves from packets and never blocks: the slow capture runs at most a
        # couple of times, off the loop, and all moves still commit.
        phone = FakePhone(board=OPEN_BOARD)
        source = FakeBoardSource(phone)  # always-healthy packets

        original_screenshot = phone.screenshot
        captures = _Counter()

        def slow_screenshot() -> Any:
            captures.bump()
            time.sleep(0.3)
            return original_screenshot()

        phone.screenshot = slow_screenshot  # type: ignore[method-assign]

        h = make_harness(
            tmp_path,
            phone,
            strategy=FirstLegalStrategy(),
            config=LiveConfig(
                experiment_id="t",
                max_games=1,
                max_moves_per_game=5,
                settle_stable_frames=1,
                apk_detect_every_moves=1,
                apk_detect_interval_s=0.02,  # real background worker, fast cadence
            ),
            board_source=source,
        )
        t0 = time.monotonic()
        h.runner.run()
        elapsed = time.monotonic() - t0

        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 5  # every move committed despite the slow detect
        # Far fewer captures than the 6+ a synchronous per-move tick would make,
        # and total time nowhere near 6 * 0.3 s -- the loop was not serialized
        # behind the background screencap. (+1 budget for the intentional fresh
        # terminal capture `_save_game_screen` takes once at game finalization,
        # off the move hot path.)
        assert captures.value <= 4
        assert elapsed < 1.5
        assert [e.end_reason for e in _events(h.recorder, GameEnded)] == ["aborted"]  # type: ignore[attr-defined]


class _Counter:
    """Thread-safe call counter shared between the loop and the worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.value = 0

    def bump(self) -> None:
        with self._lock:
            self.value += 1

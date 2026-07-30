"""Live loop driven by an on-device APK board source (fast packet perception).

All offline: the :class:`FakeBoardSource` returns
:class:`~bot2048.device.apk_capture.BoardSample` objects (the phone's real
engine board), so the loop's packet gate, its full-frame fail-closed confirm,
the periodic control tick, and the silent-link fallback are all exercised
without adb or a phone.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot2048.core.types import Board, Direction, Spawn
from bot2048.device.apk_capture import BoardSample
from bot2048.recording import GameEnded, MovePlayed, TileSpawned
from bot2048.runner.live import LiveConfig, _GameState
from tests.runner.conftest import (
    DEAD_BOARD,
    NEARLY_DEAD_BOARD,
    PREDICTED_AFTER_RIGHT,
    FakePhone,
    ScriptedStrategy,
    make_harness,
)

#: A playable two-tile board: RIGHT merges the pair, the game is NOT over.
START_BOARD: Board = (1, 1) + (0,) * 14


@dataclass
class FakeBoardSource:
    """Scripted APK board source. Default: report the phone's live board.

    Each ``poll`` pops one optional override from ``script``: ``None`` (silent
    link -> loop falls back to a full frame), ``"not_in_game"`` /
    ``"secure"`` (an off-board packet the loop must confirm with a full frame),
    or an explicit :data:`Board` (returned as a trustworthy sample). With no
    override it returns the phone's current engine board as a healthy,
    board-changed sample -- exactly what the real APK emits once a swipe settles.
    """

    phone: FakePhone
    script: deque[Any] = field(default_factory=deque)
    polls: int = 0
    started: bool = False
    closed: bool = False

    def start(self) -> None:
        self.started = True

    def poll(self) -> BoardSample | None:
        self.polls += 1
        item: Any = self.script.popleft() if self.script else "live"
        if item is None:
            return None
        if item == "not_in_game":
            return self._sample(self.phone.board, not_in_game=True, changed=False)
        if item == "secure":
            return self._sample(self.phone.board, secure=True, changed=False)
        board: Board = self.phone.board if item == "live" else item
        return self._sample(board, changed=True)

    def _sample(
        self,
        board: Board,
        *,
        changed: bool = False,
        not_in_game: bool = False,
        secure: bool = False,
    ) -> BoardSample:
        return BoardSample(
            board=board,
            seq=self.polls,
            changed=changed,
            not_in_game=not_in_game,
            secure_suspected=secure,
            uncertain=False,
        )

    def close(self) -> None:
        self.closed = True


def _events(recorder: Any, kind: type) -> list[Any]:
    return [e for e in recorder.events if isinstance(e, kind)]


def _alerts(path: Path) -> list[str]:
    if not path.exists():
        return []
    import json

    return [json.loads(line)["code"] for line in path.read_text(encoding="utf-8").splitlines()]


# -- the happy path: the board comes from the packet, not from vision ----------


class TestBoardFromPacket:
    def test_move_verified_from_packet_without_frame_parse(self, tmp_path: Path) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        # Spy on the vision parse: with settle_stable_frames=1 it is called
        # EXACTLY once, to acquire the initial board -- never for the move,
        # which is verified straight from the packet gate.
        read_calls = {"n": 0}
        original_read = phone.read_board

        def counting_read(image: Any) -> Board:
            read_calls["n"] += 1
            return original_read(image)

        phone.read_board = counting_read  # type: ignore[method-assign]
        source = FakeBoardSource(phone)
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(
                experiment_id="t",
                max_games=1,
                settle_stable_frames=1,
                apk_detect_every_moves=0,  # isolate the gate: no control tick
            ),
            board_source=source,
        )
        h.runner.run()

        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 1
        assert moves[0].board_after == PREDICTED_AFTER_RIGHT  # pre-spawn prediction
        spawns = [e for e in _events(h.recorder, TileSpawned) if e.ply >= 0]
        assert [s.spawn for s in spawns] == [Spawn(cell=0, exponent=1)]
        assert spawns[0].board_after == DEAD_BOARD  # the packet board, verbatim
        assert _events(h.recorder, GameEnded)[0].end_reason == "game_over"

        assert source.polls >= 1  # the packet gate drove verification
        assert read_calls["n"] == 1  # vision only touched the initial board
        assert phone.swipes == [Direction.RIGHT]  # one clean swipe, no retries
        assert phone.taps == []
        assert _alerts(h.alerts_path) == []


# -- fail-closed: an off-board packet is confirmed with a full frame -----------


class TestFailClosed:
    def test_not_in_game_packet_confirms_gameover_via_full_frame(
        self, tmp_path: Path
    ) -> None:
        # The fatal swipe fills the board; the app shows «Гру завершено» and the
        # sampler flags NOT_IN_GAME (popup covers the board). The loop must NOT
        # trust the packet board -- it pulls a full frame, sees the popup, and
        # finalizes game_over. The single poll proves the confirm was immediate
        # (no gate spin, no blind tap).
        phone = FakePhone(
            board=START_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            game_over_after_swipes=1,
        )
        source = FakeBoardSource(phone, script=deque(["not_in_game"]))
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, apk_detect_every_moves=0),
            board_source=source,
        )
        h.runner.run()

        ended = _events(h.recorder, GameEnded)
        assert [e.end_reason for e in ended] == ["game_over"]
        assert _events(h.recorder, MovePlayed) == []  # popup hid the after-board
        assert source.polls == 1  # not_in_game -> immediate full-frame confirm
        assert phone.taps == []  # no restart budgeted, no blind tap
        assert _alerts(h.alerts_path) == []  # natural end: no alerts


# -- silent / dropped link falls back to the full-frame verification -----------


class TestSilentLinkFallback:
    def test_poll_none_falls_back_to_frames_and_still_verifies(
        self, tmp_path: Path
    ) -> None:
        phone = FakePhone(
            board=NEARLY_DEAD_BOARD, spawn_script=deque([Spawn(cell=0, exponent=1)])
        )
        source = FakeBoardSource(phone, script=deque([None]))  # link silent
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(experiment_id="t", max_games=1, apk_detect_every_moves=0),
            board_source=source,
        )
        h.runner.run()

        moves = _events(h.recorder, MovePlayed)
        assert len(moves) == 1  # verified via the full-frame fallback
        assert moves[0].direction is Direction.RIGHT
        spawns = [e for e in _events(h.recorder, TileSpawned) if e.ply >= 0]
        assert [s.spawn for s in spawns] == [Spawn(cell=0, exponent=1)]
        assert _events(h.recorder, GameEnded)[0].end_reason == "game_over"
        assert source.polls == 1  # tried the packet once, then fell back
        assert phone.swipes == [Direction.RIGHT]  # no MOVE_REJECTED / blind retry
        assert _alerts(h.alerts_path) == []

    def test_transient_lull_keeps_polling_and_avoids_a_full_frame(
        self, tmp_path: Path
    ) -> None:
        # A momentary empty poll (a WiFi read-timeout) must NOT immediately pay a
        # ~1.3 s full-frame screencap: the gate stays on the cheap packet path
        # until the settled spawn packet arrives, so no foreground capture runs.
        # (Regression guard for the APK capture tail; the shared harness clock
        # jumps 1 s/read and would trip the budget on the first re-check, so this
        # unit-drives ``_observe_via_apk`` with a fine-grained clock instead.)
        predicted: Board = (0,) + (3,) * 15
        settled: Board = (1,) + (3,) * 15  # predicted + one settled "2" spawn
        phone = FakePhone(board=predicted)
        source = FakeBoardSource(phone, script=deque([None, None, settled]))
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT]),
            config=LiveConfig(
                experiment_id="t", max_games=1, apk_detect_every_moves=0
            ),
            board_source=source,
        )

        tick = {"t": 0.0}

        def fine_clock() -> float:
            tick["t"] += 0.001  # 1 ms/read: many polls fit the 0.8 s budget
            return tick["t"]

        h.runner._clock = fine_clock  # type: ignore[method-assign]
        h.runner._timer.reset()
        h.runner._counters.reset()
        state = _GameState(game_id="g", board=predicted, t0=0.0)
        state.ply = 1

        result = h.runner._observe_via_apk(state, predicted)

        assert result == ("spawn", settled)  # gate hit on the late packet
        assert source.polls == 3  # two lulls, then the settled packet
        assert h.runner._counters.capture_count == 0  # NO foreground full frame


# -- synchronous fallback tick catches what the packet cannot see ---------------


class TestControlTick:
    def test_periodic_full_frame_detects_gameover_popup(self, tmp_path: Path) -> None:
        # The packets stay "healthy" (the sampler never flags the popup), so
        # ONLY the periodic full-frame control tick can catch the «Гру
        # завершено» popup the app raised after move 1. apk_detect_interval_s=0
        # disables the async background worker, so the SYNCHRONOUS fallback tick
        # runs (cadence=1 -> at the top of move 2, before any swipe) -- the
        # pre-async behavior, verifying a missing worker is never worse.
        phone = FakePhone(
            board=START_BOARD,
            spawn_script=deque([Spawn(cell=0, exponent=1)]),
            game_over_after_swipes=1,
        )
        source = FakeBoardSource(phone)  # always healthy packets
        h = make_harness(
            tmp_path,
            phone,
            strategy=ScriptedStrategy([Direction.RIGHT, Direction.RIGHT]),
            config=LiveConfig(
                experiment_id="t",
                max_games=1,
                settle_stable_frames=1,
                apk_detect_every_moves=1,  # control tick every move
                apk_detect_interval_s=0.0,  # no async worker: synchronous fallback
            ),
            board_source=source,
        )
        h.runner.run()

        # move 1 committed from the packet; move 2's control tick found the popup
        assert len(_events(h.recorder, MovePlayed)) == 1
        assert _events(h.recorder, GameEnded)[0].end_reason == "game_over"
        assert phone.swipes == [Direction.RIGHT]  # move 2 never swiped (tick fired first)
        assert _alerts(h.alerts_path) == []

"""Fakes and fixtures for the live-loop tests.

The loop runs against :class:`FakePhone` -- an in-memory phone with REAL
engine dynamics (swipes apply ``core.engine.move``) and scripted/deterministic
spawns, plus the REAL :class:`bot2048.device.supervisor.Supervisor` (with
injected no-op sleep and a tmp incidents root). No adb, no real delays.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from bot2048.ai.strategies import Decision
from bot2048.core import engine
from bot2048.core.types import Board, Direction, Spawn
from bot2048.device import ui_map
from bot2048.device.adb import EmptyScreencapError
from bot2048.device.humanizer import BBox, Humanizer, SwipeParams
from bot2048.device.supervisor import Supervisor, SupervisorConfig
from bot2048.recording import ListRecorder
from bot2048.runner.alerts import AlertWriter
from bot2048.runner.incidents import MoveRing
from bot2048.runner.live import CellLook, LiveConfig, LiveRunner
from bot2048.vision.states import ScreenState, StateDetection

Image = npt.NDArray[np.uint8]

GRID_BBOX: BBox = (100, 660, 980, 1540)

#: One RIGHT move + a spawn at cell 0 (exp 1) turns this board into DEAD_BOARD.
NEARLY_DEAD_BOARD: Board = (2, 1, 2, 0, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)
PREDICTED_AFTER_RIGHT: Board = (0, 2, 1, 2, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)
DEAD_BOARD: Board = (1, 2, 1, 2, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)

#: A fresh two-tile board the fake phone produces after the restart flow.
FRESH_BOARD: Board = (1,) + (0,) * 14 + (1,)


class ScriptedStrategy:
    """Plays a fixed direction sequence (satisfies the ai Strategy protocol)."""

    def __init__(self, directions: Iterable[Direction]) -> None:
        self._directions = deque(directions)

    @property
    def name(self) -> str:
        return "scripted"

    def choose(self, board: Board) -> Decision:
        return Decision(direction=self._directions.popleft(), eval_ms=0.5)


@dataclass
class FakePhone:
    """In-memory device + vision: real move dynamics, scripted spawns.

    ``state`` is what the (fake) detector reports: ``game`` | ``confirm`` |
    ``auth`` | ``off`` | ``win`` («Ви зібрали 2048!» popup; its orange «Нова
    гра» tap restarts directly) | ``gameover`` («Гру завершено» popup; its
    «Нова гра» tap -- the SAME point -- restarts directly) | ``broken`` (an
    UNKNOWN screen that never recovers -> supervisor FATAL). Swipes apply
    ``engine.move`` and then a spawn: the
    next scripted one whose cell is empty, otherwise the first empty cell
    (exponent 1). Every applied spawn is appended to ``spawn_log`` so tests
    can compare it against the recorded ``TileSpawned`` events.
    """

    board: Board
    spawn_script: deque[Spawn] = field(default_factory=deque)
    state: str = "game"
    fresh_board: Board = FRESH_BOARD
    reject_swipes: int = 0  # ignore this many swipes (MOVE_REJECTED scenarios)
    corrupt_to: Board | None = None  # swipe result forced to this (VISION_MISMATCH)
    animate_after_swipe: tuple[Board, ...] = ()  # transient frames after each applied swipe
    animate_reads: deque[Board] = field(default_factory=deque)  # pending transient reads
    auth_after_swipes: int | None = None  # switch to the auth wall after N swipes
    break_after_swipes: int | None = None  # screen goes UNKNOWN after N swipes (FATAL)
    win_after_swipes: int | None = None  # «Ви зібрали 2048!» popup after N swipes
    game_over_after_swipes: int | None = None  # «Гру завершено» popup after N swipes
    #: Model an expired token on restart: the win/game-over «Нова гра» tap lands
    #: on the auth wall instead of a fresh board; the PIN then reveals the fresh
    #: game (deferred via ``_pending_fresh_after_auth``).
    new_game_needs_auth: bool = False
    #: Model a KNOWN terminal popup hiding UNDER the auth wall: once the PIN
    #: clears the wall the screen shows this state ("win" / "gameover" /
    #: "broken") instead of a fresh "game". Drives the auth-recovery /
    #: startup-acquire terminal-popup tests. None = pre-existing behavior (PIN
    #: reveals the game, honoring ``_pending_fresh_after_auth``).
    pin_reveals_state: str | None = None
    pin: str = "1234"  # accepted PIN length driver for the fake pin pad
    #: Number of FULL PIN entries the wall demands before it clears (models a
    #: flaky post-idle auth wall). 1 = the pre-existing behavior (the first
    #: entry clears). >1 keeps the screen on "auth" until that many entries have
    #: been typed, exercising the bounded ``auth_retries`` PIN-retry loop.
    pin_entries_to_clear: int = 1
    # -- auto-learn scenario knobs (read_cells / learn_tile fakes) --------------
    misread: dict[int, int] = field(default_factory=dict)  # cell -> exponent vision reports
    #: true exponent -> reported exponent, applied to ANY cell holding it: models a
    #: tile VALUE vision has never seen (run3: the pink 1024 read as unknown/0 while
    #: the 512 pair stayed perfectly readable). Cleared by a successful learn_tile.
    misread_values: dict[int, int] = field(default_factory=dict)
    suspect_cells: set[int] = field(default_factory=set)  # color-fallback / low-confidence
    learn_accepts: bool = True  # learn_tile verdict (False = sample refused)
    # -- transient-error injection ----------------------------------------------
    crash_after_swipes: int | None = None  # arm ONE transient screenshot error after N swipes
    transient_errors: deque[Exception] = field(default_factory=deque)  # next screenshot raises

    swipes: list[Direction] = field(default_factory=list)
    taps: list[tuple[int, int]] = field(default_factory=list)
    spawn_log: list[Spawn] = field(default_factory=list)
    learn_calls: list[tuple[int, int]] = field(default_factory=list)  # (cell, exponent)
    _pin_taps: int = 0
    _pin_entries_done: int = 0  # full PIN entries typed on the current wall
    _frame_no: int = 0
    _pending_fresh_after_auth: bool = False  # PIN reveals a fresh board (token expiry)

    # -- LiveDevice / SupervisorDevice protocol --------------------------------

    def screenshot(self) -> Image:
        if self.transient_errors:
            raise self.transient_errors.popleft()
        if self.state == "auth":
            raise EmptyScreencapError("screencap 0 bytes (fake FLAG_SECURE)")
        self._frame_no += 1
        return np.full((8, 8, 3), self._frame_no % 251, dtype=np.uint8)

    def ui_dump(self) -> str:
        anchor = ui_map.AUTH_TEXT_ANCHOR if self.state == "auth" else "game"
        return f"<hierarchy>{anchor}</hierarchy>"

    def tap(self, x: int, y: int, *, sensitive: bool = False) -> None:
        self.taps.append((x, y))
        if self.state == "auth":
            if (x, y) in ui_map.PIN_KEYS.values():
                self._pin_taps += 1
                if self._pin_taps >= len(self.pin):
                    self._pin_taps = 0
                    self._pin_entries_done += 1
                    if self._pin_entries_done < self.pin_entries_to_clear:
                        return  # flaky wall: this full PIN entry did not clear it
                    if self.pin_reveals_state is not None:
                        # A KNOWN terminal popup was hiding behind the wall: the
                        # PIN clears the auth screen and reveals it.
                        self.state = self.pin_reveals_state
                    elif self._pending_fresh_after_auth:
                        # A restart that routed through re-auth: the fresh game
                        # only appears once the PIN clears the wall.
                        self.state = "game"
                        self.board = self.fresh_board
                        self._pending_fresh_after_auth = False
                    else:
                        self.state = "game"
            return
        if (x, y) == ui_map.NEW_GAME and self.state == "game":
            self.state = "confirm"
        elif (x, y) == ui_map.CONFIRM_YES and self.state == "confirm":
            self.state = "game"
            self.board = self.fresh_board
        elif (x, y) == ui_map.CONTINUE_GAME and self.state == "confirm":
            self.state = "game"
        elif (x, y) == ui_map.WIN_NEW_GAME and self.state in ("win", "gameover"):
            # The win / game-over popup «Нова гра» (same point): fresh game,
            # no confirmation step -- unless an expired token routes it through
            # the auth wall first (the fresh board waits for the PIN).
            if self.new_game_needs_auth:
                self.state = "auth"
                self._pending_fresh_after_auth = True
            else:
                self.state = "game"
                self.board = self.fresh_board

    def wake(self) -> None:
        if self.state == "off":
            self.state = "game"

    def swipe_direction(
        self, direction: Direction, grid_bbox: BBox, humanizer: Humanizer
    ) -> SwipeParams:
        params = humanizer.swipe_params(direction, grid_bbox)
        self.swipes.append(direction)
        if self.reject_swipes > 0:
            self.reject_swipes -= 1
            return params
        result = engine.move(self.board, direction)
        if result.moved:
            if self.corrupt_to is not None:
                self.board = self.corrupt_to
            else:
                self.board = self._apply_spawn(result.board)
            # Mid-merge animation: the next reads show these transient boards
            # before the settled self.board becomes visible.
            self.animate_reads.extend(self.animate_after_swipe)
        if self.auth_after_swipes is not None:
            self.auth_after_swipes -= 1
            if self.auth_after_swipes <= 0:
                self.state = "auth"
        if self.break_after_swipes is not None:
            self.break_after_swipes -= 1
            if self.break_after_swipes <= 0:
                self.state = "broken"
        if self.win_after_swipes is not None:
            self.win_after_swipes -= 1
            if self.win_after_swipes <= 0:
                self.win_after_swipes = None
                self.state = "win"  # popup covers the board before verification
        if self.game_over_after_swipes is not None:
            self.game_over_after_swipes -= 1
            if self.game_over_after_swipes <= 0:
                self.game_over_after_swipes = None
                self.state = "gameover"  # popup covers the board before verification
        if self.crash_after_swipes is not None:
            self.crash_after_swipes -= 1
            if self.crash_after_swipes <= 0:
                self.crash_after_swipes = None
                self.transient_errors.append(
                    RuntimeError("adb: transient screencap failure (fake)")
                )
        return params

    def _apply_spawn(self, after_move: Board) -> Board:
        spawn: Spawn | None = None
        if self.spawn_script and after_move[self.spawn_script[0].cell] == 0:
            spawn = self.spawn_script.popleft()
        else:
            empties = engine.empty_cells(after_move)
            assert empties, "fake phone: no empty cell to spawn into"
            spawn = Spawn(cell=empties[0], exponent=1)
        cells = list(after_move)
        cells[spawn.cell] = spawn.exponent
        self.spawn_log.append(spawn)
        return tuple(cells)

    # -- fake vision -------------------------------------------------------------

    def detect(self, image: Image) -> StateDetection:
        if self.state == "confirm":
            return StateDetection(ScreenState.CONFIRM_NEW_GAME, "fake popup", 0.0)
        if self.state == "off":
            return StateDetection(ScreenState.SCREEN_OFF, "fake dark frame", 0.0)
        if self.state == "win":
            return StateDetection(ScreenState.WIN_POPUP_2048, "fake win popup", 0.0)
        if self.state == "gameover":
            return StateDetection(ScreenState.GAME_OVER_POPUP, "fake game over popup", 0.0)
        if self.state == "broken":
            return StateDetection(ScreenState.UNKNOWN, "fake broken screen", 0.0)
        return StateDetection(ScreenState.GAME, "fake board", 1.0)

    def read_board(self, image: Image) -> Board:
        board = self.animate_reads.popleft() if self.animate_reads else self.board
        return self._overlay_misreads(board)

    def _overlay_misreads(self, board: Board) -> Board:
        """What vision REPORTS: occupied ``misread`` cells show the wrong exponent."""
        if not self.misread and not self.misread_values:
            return board
        cells = list(board)
        for cell, exponent in self.misread.items():
            if cells[cell]:
                cells[cell] = exponent
        for i, e in enumerate(cells):
            if e in self.misread_values:
                cells[i] = self.misread_values[e]
        return tuple(cells)

    def read_cells(self, image: Image) -> tuple[CellLook, ...]:
        """CellsReader fake: per-cell trust detail for the settled board."""
        reported = self._overlay_misreads(self.board)
        return tuple(
            CellLook(
                exponent=e,
                suspect=(
                    i in self.suspect_cells
                    # a value vision has never seen reads as unknown -> ALWAYS suspect
                    or (self.board[i] != 0 and self.board[i] in self.misread_values)
                ),
            )
            for i, e in enumerate(reported)
        )

    def learn_tile(self, frame: Image, cell: int, exponent: int) -> bool:
        """LearnTileHook fake: on accept, the calibration now knows the tile."""
        self.learn_calls.append((cell, exponent))
        if not self.learn_accepts:
            return False
        self.misread.pop(cell, None)  # future reads see the true exponent
        self.misread_values.pop(exponent, None)  # the value's color is now known
        self.suspect_cells.discard(cell)
        return True


@dataclass
class FakeHeartbeat:
    updates: list[tuple[str | None, int | None]] = field(default_factory=list)

    def update(self, *, game_id: str | None = None, ply: int | None = None) -> None:
        self.updates.append((game_id, ply))


@dataclass
class Harness:
    phone: FakePhone
    runner: LiveRunner
    recorder: ListRecorder
    alerts_path: Path
    heartbeat: FakeHeartbeat
    notifications: list[tuple[str, str, str]]
    #: (level, code, caption, photo_path) for every photo notification.
    photo_notifications: list[tuple[str, str, str, str]]


def make_harness(
    tmp_path: Path,
    phone: FakePhone,
    *,
    strategy: Any,
    config: LiveConfig,
    recorder: Any | None = None,
    auto_pin: str | None = None,
    read_cells: Any | None = None,
    learn_tile: Any | None = None,
    reconnect: Any | None = None,
    board_source: Any | None = None,
    bg_detect: Any | None = None,
) -> Harness:
    """Wire a LiveRunner around ``phone`` with the REAL Supervisor, no delays."""
    alerts_path = tmp_path / "alerts.jsonl"
    alerts = AlertWriter(alerts_path)
    supervisor = Supervisor(
        phone,
        phone.detect,
        alerts,
        SupervisorConfig(incidents_root=tmp_path / "incidents"),
        sleep=lambda _s: None,
    )
    rec = recorder if recorder is not None else ListRecorder()
    heartbeat = FakeHeartbeat()
    notifications: list[tuple[str, str, str]] = []
    photo_notifications: list[tuple[str, str, str, str]] = []

    def _notify(level: str, code: str, text: str, photo_path: str | None = None) -> None:
        if photo_path is None:
            notifications.append((level, code, text))
        else:
            photo_notifications.append((level, code, text, photo_path))
    runner = LiveRunner(
        device=phone,
        detect_state=phone.detect,
        read_board=phone.read_board,
        board_source=board_source,
        strategy=strategy,
        recorder=rec,
        supervisor=supervisor,
        alerts=alerts,
        grid_bbox=GRID_BBOX,
        humanizer=Humanizer(rng=random.Random(7)),
        config=config,
        run_id="run-test",
        heartbeat=heartbeat,
        ring=MoveRing(),
        notify=_notify,
        pause_flag=tmp_path / "pause.flag",
        pin_reader=lambda: auto_pin,
        read_cells=read_cells,
        learn_tile=learn_tile,
        reconnect=reconnect,
        bg_detect=bg_detect,
        incidents_root=tmp_path / "incidents",
        screens_root=tmp_path / "game-screens",
        # Every runtime artifact root under tmp_path: a test run must never
        # litter the real project/logs/ (debug-captures used to leak there).
        debug_root=tmp_path / "debug-captures",
        sleep=lambda _s: None,
        clock=_FakeClock(),
    )
    return Harness(  # type: ignore[arg-type]
        phone, runner, rec, alerts_path, heartbeat, notifications, photo_notifications
    )


class _FakeClock:
    def __init__(self) -> None:
        self._t = 1000.0

    def __call__(self) -> float:
        self._t += 1.0
        return self._t

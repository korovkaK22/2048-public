"""Transient-occlusion slow retry before a VISION_MISMATCH abort.

A physical object (a cat's paw) can occlude 1-2 board cells for a few
seconds; the fast animation loop parses garbage and the game would be lost
as ``vision_mismatch``. :meth:`SwipeController._occlusion_retry` waits it out
with slow fresh-recapture retries. These tests drive the real ``play_move``
with a fake clock/sleep (no real 15 s waits) and a scripted fake gate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from structlog.testing import capture_logs

from bot2048.ai.strategies import Decision
from bot2048.core import engine
from bot2048.core.types import Board, Direction
from bot2048.device.humanizer import BBox, Humanizer, HumanizerConfig, SwipeParams
from bot2048.recording import GameEvent, MovePlayed
from bot2048.runner.live import CellLook, LiveConfig, _GameState
from bot2048.runner.move_telemetry import _MoveCounters, _MovePhaseTimer
from bot2048.runner.swipe_controller import SwipeController

GRID_BBOX: BBox = (100, 660, 980, 1540)

#: two exp-1 tiles that a RIGHT swipe slides into the rightmost column
#: (cells 3 and 7) -- so the after-swipe PREDICTED board HOLDS a tile there.
PRE_BOARD: Board = (1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
#: engine.move(PRE_BOARD, RIGHT) -- the pre-spawn predicted board (tiles @ 3,7).
PREDICTED: Board = engine.move(PRE_BOARD, Direction.RIGHT).board
#: predicted + one settled exp-1 spawn at cell 0 (a clean, reconciling read).
CLEAN_AFTER: Board = (1, *PREDICTED[1:])
#: neither PRE_BOARD nor predicted+spawn -> a genuine mismatch parse.
OCCLUDED_MISREAD: Board = tuple(2 if cell in (3, 7) else 0 for cell in range(16))
#: PREDICTED-NON-EMPTY cells that read unreadable = a GENUINE occlusion (a tile
#: that should be there but the cat-mascot paw covers it). Under the corrected
#: rule (commit f120109) only these trigger the slow occlusion retry.
OCCLUDED_CELLS = [3, 7]
#: PREDICTED-EMPTY cells that read suspect (no digit to read) -- NOT occlusion.
#: The LIVE ply-361/365 bug: 6-7 empty cells flagged "occluded" burned the
#: retry budget. The corrected rule ignores these -> immediate abort.
EMPTY_SUSPECT_CELLS = [0, 1]


@dataclass
class _Frame:
    board: Board
    occluded: bool


@dataclass
class _FakeGate:
    """Scripted gate: mismatch first, then N occluded recaptures, then clean."""

    captures_before_clean: int
    last_frame: _Frame | None = None
    capture_count: int = 0

    def observe_after_swipe(
        self, state: _GameState, predicted: Board
    ) -> tuple[str, Board]:
        self.last_frame = _Frame(board=OCCLUDED_MISREAD, occluded=True)
        return ("mismatch", OCCLUDED_MISREAD)

    def capture_game_frame(self, state: _GameState | None) -> _Frame:
        self.capture_count += 1
        if self.capture_count >= self.captures_before_clean:
            frame = _Frame(board=CLEAN_AFTER, occluded=False)
        else:
            frame = _Frame(board=OCCLUDED_MISREAD, occluded=True)
        self.last_frame = frame
        return frame

    def parse_or_escalate(
        self, frame: _Frame, state: _GameState | None = None
    ) -> Board:
        return frame.board


@dataclass
class _CleanGate(_FakeGate):
    """Mismatch whose frame is fully READABLE (a real, non-occluded mismatch)."""

    def observe_after_swipe(
        self, state: _GameState, predicted: Board
    ) -> tuple[str, Board]:
        self.last_frame = _Frame(board=OCCLUDED_MISREAD, occluded=False)
        return ("mismatch", OCCLUDED_MISREAD)


def _make_read_cells(
    suspect_cells: list[int],
) -> Callable[[_Frame], tuple[CellLook, ...]]:
    """A CellsReader marking ``suspect_cells`` suspect on an occluded frame."""

    def _read(frame: _Frame) -> tuple[CellLook, ...]:
        return tuple(
            CellLook(
                exponent=e,
                suspect=(frame.occluded and cell in suspect_cells),
            )
            for cell, e in enumerate(frame.board)
        )

    return _read


#: Default reader: the genuine (predicted-non-empty) occlusion cells.
_read_cells = _make_read_cells(OCCLUDED_CELLS)


class _FakeStrategy:
    def choose(self, board: Board) -> Decision:
        return Decision(direction=Direction.RIGHT, eval_ms=0.5)


class _FakeDevice:
    def swipe_direction(
        self, direction: Direction, grid_bbox: BBox, humanizer: Humanizer
    ) -> SwipeParams:
        return humanizer.swipe_params(direction, grid_bbox)


@dataclass
class _FakeSupervisor:
    errors: list[dict[str, Any]] = field(default_factory=list)

    def handle_error(self, code: Any, message: str, **kwargs: Any) -> None:
        self.errors.append({"code": code, "message": message, **kwargs})


@dataclass
class _Clock:
    """Fake monotonic clock advanced only by the fake sleep (no real waits)."""

    now: float = 100.0

    def read(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _make_controller(
    gate: _FakeGate,
    *,
    config: LiveConfig,
    clock: _Clock,
    records: list[GameEvent],
    supervisor: _FakeSupervisor,
    read_cells: Callable[[_Frame], tuple[CellLook, ...]] = _read_cells,
) -> SwipeController:
    timer = _MovePhaseTimer(clock.read)
    return SwipeController(
        strategy=_FakeStrategy(),
        device=_FakeDevice(),
        grid_bbox=GRID_BBOX,
        humanizer=Humanizer(HumanizerConfig.for_profile("fast")),
        gate=gate,  # type: ignore[arg-type]
        config=config,
        supervisor=supervisor,  # type: ignore[arg-type]
        timer=timer,
        counters=_MoveCounters(),
        board_source=None,
        read_cells=read_cells,  # type: ignore[arg-type]
        learn_tile=None,
        heartbeat=None,
        ring=None,
        record=records.append,
        notify=lambda level, code, text: None,
        strategy_label=lambda: "scripted",
        control_tick=lambda state: None,
        note_move_committed=lambda: None,
        clock=clock.read,
        sleep=clock.sleep,
    )


def _state() -> _GameState:
    return _GameState(game_id="g0", board=PRE_BOARD, t0=0.0)


def test_occluded_thrice_then_clean_records_move() -> None:
    clock = _Clock()
    records: list[GameEvent] = []
    supervisor = _FakeSupervisor()
    # capture #3 is the first clean one -> 2 occluded recaptures, then success.
    gate = _FakeGate(captures_before_clean=3)
    controller = _make_controller(
        gate,
        config=LiveConfig(experiment_id="t", occlusion_retry_delay_s=3.0),
        clock=clock,
        records=records,
        supervisor=supervisor,
    )

    with capture_logs() as logs:
        result = controller.play_move(_state())

    assert result is None  # the move was verified and recorded
    moves = [e for e in records if isinstance(e, MovePlayed)]
    assert len(moves) == 1

    retries = [x for x in logs if x["event"] == "live.vision_occluded_retry"]
    giveups = [x for x in logs if x["event"] == "live.vision_occluded_giveup"]
    assert len(retries) == 3
    assert not giveups
    assert not supervisor.errors  # no VISION_MISMATCH abort
    # Timing/diagnostic fields are present on every retry warning.
    assert retries[0]["attempt"] == "1/5"
    assert retries[0]["occluded_cells"] == OCCLUDED_CELLS
    assert "elapsed_s" in retries[0]
    assert retries[1]["elapsed_s"] == 3.0  # one 3 s wait elapsed


def test_occluded_all_retries_aborts_vision_mismatch() -> None:
    clock = _Clock()
    records: list[GameEvent] = []
    supervisor = _FakeSupervisor()
    gate = _FakeGate(captures_before_clean=999)  # never clears
    controller = _make_controller(
        gate,
        config=LiveConfig(
            experiment_id="t", occlusion_retry_delay_s=3.0, occlusion_max_retries=5
        ),
        clock=clock,
        records=records,
        supervisor=supervisor,
    )

    with capture_logs() as logs:
        result = controller.play_move(_state())

    assert result == "vision_mismatch"
    assert not [e for e in records if isinstance(e, MovePlayed)]

    retries = [x for x in logs if x["event"] == "live.vision_occluded_retry"]
    giveups = [x for x in logs if x["event"] == "live.vision_occluded_giveup"]
    assert len(retries) == 5  # five waits
    assert len(giveups) == 1
    assert giveups[0]["attempts"] == 5
    assert giveups[0]["elapsed_s"] == 15.0  # 5 x 3.0 s
    assert giveups[0]["occluded_cells"] == OCCLUDED_CELLS
    assert len(supervisor.errors) == 1  # the standard VISION_MISMATCH abort


def test_readable_but_different_board_does_not_slow_retry() -> None:
    clock = _Clock()
    records: list[GameEvent] = []
    supervisor = _FakeSupervisor()
    gate = _CleanGate(captures_before_clean=999)
    controller = _make_controller(
        gate,
        config=LiveConfig(experiment_id="t"),
        clock=clock,
        records=records,
        supervisor=supervisor,
    )

    with capture_logs() as logs:
        result = controller.play_move(_state())

    # A fully-readable but genuinely different board keeps today's behavior:
    # immediate abort, no occlusion waiting.
    assert result == "vision_mismatch"
    assert not [x for x in logs if x["event"] == "live.vision_occluded_retry"]
    assert clock.now == 100.0 + 0.13  # only the swipe settle slept; no 3 s waits
    assert len(supervisor.errors) == 1


def test_empty_predicted_suspect_cells_abort_immediately() -> None:
    """LIVE ply 361/365 regression (commit f120109).

    A mismatch whose ONLY suspect cells are PREDICTED-EMPTY is NOT an occlusion
    (an empty cell reads "suspect" simply because there is no digit to read).
    The move must abort ``vision_mismatch`` AT ONCE -- zero occlusion retries,
    no ~15 s wait -- instead of burning the whole retry budget waiting out
    empty cells that will never "clear".
    """
    clock = _Clock()
    records: list[GameEvent] = []
    supervisor = _FakeSupervisor()
    # The frame is occluded-flagged, but its suspect cells (0, 1) are EMPTY in
    # the predicted-after-swipe board -> not a genuine occlusion.
    gate = _FakeGate(captures_before_clean=999)
    controller = _make_controller(
        gate,
        config=LiveConfig(
            experiment_id="t", occlusion_retry_delay_s=3.0, occlusion_max_retries=5
        ),
        clock=clock,
        records=records,
        supervisor=supervisor,
        read_cells=_make_read_cells(EMPTY_SUSPECT_CELLS),
    )

    with capture_logs() as logs:
        result = controller.play_move(_state())

    assert result == "vision_mismatch"
    # ZERO occlusion retries: the empty-cell suspects never trigger the slow
    # stage, so the game is not lost to a ~15 s wait (the ply-361 failure).
    assert not [x for x in logs if x["event"] == "live.vision_occluded_retry"]
    assert not [x for x in logs if x["event"] == "live.vision_occluded_giveup"]
    assert clock.now == 100.0 + 0.13  # only the swipe settle slept; no 3 s waits
    assert len(supervisor.errors) == 1  # the standard immediate VISION_MISMATCH


def test_defaults_are_three_seconds_times_five() -> None:
    config = LiveConfig(experiment_id="t")
    assert config.occlusion_retry_delay_s == 3.0
    assert config.occlusion_max_retries == 5

"""On-error debug bundles: screenshot PNG + APK/board ``.json`` sidecar.

When a move dies on vision we need to SEE the screen (cat-mascot? animation?
vision bug?). These tests drive the real ``SwipeController`` hooks with a
scripted gate + fake clock (no real waits) and a real :class:`DebugCapture`
writing into ``tmp_path``, asserting:

a) bundles are written on the occlusion path (first retry + give-up) and on a
   ``vision_mismatch`` abort, with the right sidecar fields;
b) the capture runs OFF the caller's thread and a screencap exception never
   propagates;
c) the prune cap keeps the directory bounded;
d) the knob defaults ON and OFF disables every hook.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from structlog.testing import capture_logs

from bot2048.ai.strategies import Decision
from bot2048.core import engine
from bot2048.core.types import Board, Direction
from bot2048.device.humanizer import BBox, Humanizer, HumanizerConfig, SwipeParams
from bot2048.recording import GameEvent
from bot2048.runner.debug_capture import DEBUG_CAPTURE_REASONS, DebugCapture
from bot2048.runner.live import CellLook, LiveConfig, _GameState
from bot2048.runner.move_telemetry import _MoveCounters, _MovePhaseTimer
from bot2048.runner.swipe_controller import SwipeController
from bot2048.storage.schema import board_to_hex

GRID_BBOX: BBox = (100, 660, 980, 1540)

#: two exp-1 tiles that a RIGHT swipe slides into the rightmost column
#: (cells 3 and 7) -- so the after-swipe PREDICTED board HOLDS a tile there.
PRE_BOARD: Board = (1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
#: engine.move(PRE_BOARD, RIGHT) -- the pre-spawn predicted board (tiles @ 3,7).
PREDICTED: Board = engine.move(PRE_BOARD, Direction.RIGHT).board
#: neither PRE_BOARD nor predicted+spawn -> a genuine mismatch parse.
OCCLUDED_MISREAD: Board = tuple(2 if cell in (3, 7) else 0 for cell in range(16))
#: PREDICTED-NON-EMPTY cells that read unreadable = a GENUINE occlusion (a tile
#: that should be there but the cat-mascot paw covers it). Under the corrected
#: rule (commit f120109) only these trigger the slow occlusion retry.
OCCLUDED_CELLS = [3, 7]


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


def _read_cells(frame: _Frame) -> tuple[CellLook, ...]:
    return tuple(
        CellLook(
            exponent=e,
            suspect=(frame.occluded and cell in OCCLUDED_CELLS),
        )
        for cell, e in enumerate(frame.board)
    )


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


@dataclass
class _Sample:
    """Duck-typed BoardSample double (the sidecar reads attrs, not the class)."""

    board: Board
    seq: int = 7
    changed: bool = True
    not_in_game: bool = False
    secure_suspected: bool = False
    uncertain: bool = False
    age_ms: float = 12.0


class _JoinableCapture(DebugCapture):
    """Real DebugCapture that remembers its workers so tests can join them."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.workers: list[threading.Thread] = []

    def capture(self, **kwargs: Any) -> threading.Thread | None:
        worker = super().capture(**kwargs)
        if worker is not None:
            self.workers.append(worker)
        return worker

    def join_all(self) -> None:
        for worker in self.workers:
            worker.join(timeout=10.0)


def _frame_png() -> Any:
    return np.zeros((8, 8, 3), dtype=np.uint8)


def _make_debug(root: Path, **kwargs: Any) -> _JoinableCapture:
    kwargs.setdefault("screenshot", _frame_png)
    kwargs.setdefault("apk_latest", lambda: _Sample(board=OCCLUDED_MISREAD))
    return _JoinableCapture(root=root, **kwargs)


def _make_controller(
    gate: _FakeGate,
    *,
    config: LiveConfig,
    clock: _Clock,
    records: list[GameEvent],
    supervisor: _FakeSupervisor,
    debug: DebugCapture | None,
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
        read_cells=_read_cells,  # type: ignore[arg-type]
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
        debug_capture=debug,
    )


def _state() -> _GameState:
    return _GameState(game_id="g0", board=PRE_BOARD, t0=0.0)


def _sidecars(root: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(root.glob("*.json"))]


# -- (a) occlusion path: first retry + give-up ------------------------------------


def test_occlusion_first_retry_and_giveup_write_bundles(tmp_path: Path) -> None:
    clock = _Clock()
    records: list[GameEvent] = []
    supervisor = _FakeSupervisor()
    gate = _FakeGate(captures_before_clean=999)  # never clears
    debug = _make_debug(tmp_path / "dbg")
    controller = _make_controller(
        gate,
        config=LiveConfig(
            experiment_id="t", occlusion_retry_delay_s=3.0, occlusion_max_retries=2
        ),
        clock=clock,
        records=records,
        supervisor=supervisor,
        debug=debug,
    )

    with capture_logs() as logs:
        result = controller.play_move(_state())
        debug.join_all()

    assert result == "vision_mismatch"
    data = _sidecars(tmp_path / "dbg")
    reasons = [d["reason"] for d in data]
    # First retry + give-up on the occlusion path, then the abort itself.
    assert reasons.count("occluded") == 2
    assert reasons.count("vision_mismatch") == 1

    first = next(d for d in data if d["context"].get("phase") == "first_retry")
    giveup = next(d for d in data if d["context"].get("phase") == "giveup")
    for bundle in (first, giveup):
        assert bundle["game_id"] == "g0"
        assert bundle["ply"] == 0
        assert bundle["context"]["direction"] == "RIGHT"
        assert bundle["context"]["occluded_cells"] == OCCLUDED_CELLS
        assert bundle["context"]["board_before"] == board_to_hex(PRE_BOARD)
        assert bundle["context"]["board_predicted"] == board_to_hex(PREDICTED)
        assert bundle["context"]["board_parsed"] == board_to_hex(OCCLUDED_MISREAD)
        # The APK packet snapshot rides along.
        assert bundle["apk"]["board"] == list(OCCLUDED_MISREAD)
        assert bundle["apk"]["seq"] == 7
        # The screenshot PNG landed beside the sidecar.
        assert bundle["screenshot"] is not None
        assert (tmp_path / "dbg" / bundle["screenshot"]).exists()
    assert giveup["context"]["attempts"] == 2

    saved = [x for x in logs if x["event"] == "live.debug_capture_saved"]
    assert len(saved) == 3  # exactly one INFO per capture


# -- (a) vision_mismatch abort ------------------------------------------------------


def test_vision_mismatch_abort_writes_bundle(tmp_path: Path) -> None:
    clock = _Clock()
    records: list[GameEvent] = []
    supervisor = _FakeSupervisor()
    gate = _CleanGate(captures_before_clean=999)  # readable -> genuine mismatch
    debug = _make_debug(tmp_path / "dbg")
    controller = _make_controller(
        gate,
        config=LiveConfig(experiment_id="t"),
        clock=clock,
        records=records,
        supervisor=supervisor,
        debug=debug,
    )

    result = controller.play_move(_state())
    debug.join_all()

    assert result == "vision_mismatch"
    assert len(supervisor.errors) == 1
    data = _sidecars(tmp_path / "dbg")
    assert [d["reason"] for d in data] == ["vision_mismatch"]
    bundle = data[0]
    assert bundle["context"]["suspect_cells"] == []  # readable frame
    assert bundle["context"]["board_before"] == board_to_hex(PRE_BOARD)
    assert bundle["context"]["board_parsed"] == board_to_hex(OCCLUDED_MISREAD)
    assert bundle["screenshot"] is not None


# -- (a) finalization sidecar beside an existing game-screen PNG --------------------


def test_write_sidecar_beside_existing_artifact(tmp_path: Path) -> None:
    debug = _make_debug(tmp_path)
    artifact = tmp_path / "g0_vision_mismatch.png"
    artifact.write_bytes(b"png")

    json_path = debug.write_sidecar(
        artifact,
        reason="vision_mismatch",
        game_id="g0",
        ply=42,
        screenshot=artifact.name,
        context={"score": 123},
    )

    assert json_path == artifact.with_suffix(".json")
    assert json_path is not None and json_path.exists()
    bundle = json.loads(json_path.read_text())
    assert bundle["reason"] == "vision_mismatch"
    assert bundle["ply"] == 42
    assert bundle["screenshot"] == artifact.name
    assert bundle["context"] == {"score": 123}
    assert bundle["apk"]["board"] == list(OCCLUDED_MISREAD)
    # win_2048 / game_over are deliberately NOT sidecar reasons.
    assert {
        "vision_mismatch",
        "screen_unexpected",
        "game_over_unexpected",
        "device_lost",
        "error",
    } == DEBUG_CAPTURE_REASONS


# -- (b) off-thread + exception isolation -------------------------------------------


def test_capture_runs_off_thread_and_swallows_screencap_errors(
    tmp_path: Path,
) -> None:
    capture_thread_ids: list[int] = []

    def _boom() -> Any:
        capture_thread_ids.append(threading.get_ident())
        raise RuntimeError("device gone")

    debug = _make_debug(tmp_path, screenshot=_boom)

    worker = debug.capture(reason="occluded", game_id="g0", ply=3)
    assert worker is not None
    worker.join(timeout=10.0)

    # The screencap ran on the worker, not the caller's thread.
    assert capture_thread_ids and capture_thread_ids[0] != threading.get_ident()
    # The failure is recorded in the sidecar, never raised.
    (bundle,) = _sidecars(tmp_path)
    assert bundle["screenshot"] is None
    assert "RuntimeError" in bundle["screenshot_error"]
    assert bundle["apk"]["board"] == list(OCCLUDED_MISREAD)


def test_screencap_error_does_not_break_the_move(tmp_path: Path) -> None:
    clock = _Clock()
    records: list[GameEvent] = []
    supervisor = _FakeSupervisor()
    gate = _FakeGate(captures_before_clean=999)
    debug = _make_debug(tmp_path, screenshot=lambda: (_ for _ in ()).throw(OSError))
    controller = _make_controller(
        gate,
        config=LiveConfig(
            experiment_id="t", occlusion_retry_delay_s=3.0, occlusion_max_retries=2
        ),
        clock=clock,
        records=records,
        supervisor=supervisor,
        debug=debug,
    )

    result = controller.play_move(_state())  # must not raise
    debug.join_all()

    assert result == "vision_mismatch"  # the loop's own verdict, undisturbed
    assert len(_sidecars(tmp_path)) == 3  # sidecars still written


# -- (c) prune cap --------------------------------------------------------------------


def test_prune_keeps_directory_bounded(tmp_path: Path) -> None:
    # screenshot=None -> one .json per capture, so the arithmetic is exact.
    debug = _make_debug(tmp_path, screenshot=lambda: None, keep_files=3)

    for ply in range(6):
        worker = debug.capture(reason="occluded", game_id="g0", ply=ply)
        assert worker is not None
        worker.join(timeout=10.0)

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert len(remaining) == 3
    # The NEWEST captures (seq 004..006) survive; the oldest were pruned.
    assert [name.split("_")[1] for name in remaining] == ["004", "005", "006"]


# -- (d) default ON / OFF disables ----------------------------------------------------


def test_knob_defaults_on_and_off_disables(tmp_path: Path) -> None:
    assert LiveConfig(experiment_id="t").debug_capture_on_error is True
    assert LiveConfig(experiment_id="t").debug_capture_keep == 200

    debug = _make_debug(tmp_path, enabled=False)
    assert debug.capture(reason="occluded", game_id="g0", ply=1) is None
    artifact = tmp_path / "g0_error.png"
    assert (
        debug.write_sidecar(artifact, reason="error", game_id="g0", ply=1) is None
    )
    assert list(tmp_path.iterdir()) == []  # nothing written at all

"""Module-level telemetry helpers for the live loop (refactor plan R1).

Extracted VERBATIM from :mod:`bot2048.runner.live` -- pure, module-level
accumulators plus the game-end board statistics. Nothing here touches ``self``:
the live loop owns instances of these and seals their :meth:`snapshot` results
into the free-form ``MovePlayed.eval_details`` (``timing`` / ``move`` buckets)
and the ops ``live_events.details_json`` field, so the frozen
``recording``/storage contracts stay untouched.

The ``eval_details`` sub-dict keys (``timing`` phase names, ``move`` counter
names) are a DE-FACTO read-side contract for ``bot2048 db latency`` -- do NOT
rename them here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Any

from bot2048.core.types import Board, Direction
from bot2048.device.humanizer import BBox, Humanizer, SwipeParams
from bot2048.runner.incidents import Image
from bot2048.vision.states import StateDetection

if TYPE_CHECKING:
    from collections.abc import Callable

    # Runtime cycle: live.py imports this module, so LiveDevice / StateDetector
    # (defined there) are pulled in for typing only. With `from __future__ import
    # annotations` every annotation below is a string, so nothing here evaluates
    # them at runtime and there is no import cycle at load time.
    from bot2048.runner.live import LiveDevice, StateDetector


@dataclass(frozen=True, slots=True)
class _BoardStats:
    """Summary of a FINAL board for game-end telemetry.

    ``histogram`` maps each present tile VALUE (2, 4, 8, ..., 1024, ...) to how
    many of it sit on the board, ordered by descending value; ``highest`` is the
    largest tile value (``0`` for an empty board); ``empty`` is the number of
    blank cells; ``total`` is the sum of every tile value on the board.
    """

    histogram: dict[int, int]
    highest: int
    empty: int
    total: int


def _board_stats(board: Board) -> _BoardStats:
    """Tile histogram + summary of ``board`` (exponent 0 = empty cell).

    The measurement instrument behind the game-over statistics: given the last
    known parsed board it counts each tile value, the empties, the highest tile
    and the value sum -- pure, so it is trivially testable against a fixed board.
    """
    counts: dict[int, int] = {}
    empty = 0
    total = 0
    for exponent in board:
        if exponent == 0:
            empty += 1
            continue
        value = 1 << exponent  # 2 ** exponent
        counts[value] = counts.get(value, 0) + 1
        total += value
    histogram = {value: counts[value] for value in sorted(counts, reverse=True)}
    highest = next(iter(histogram), 0)
    return _BoardStats(histogram=histogram, highest=highest, empty=empty, total=total)


# -- per-move latency telemetry (docs/research/latency_telemetry.md) -----------------
#
# Where the wall-clock of ONE move goes, phase by phase, written into the
# free-form ``MovePlayed.eval_details["timing"]`` bucket at commit time -- the
# frozen recording/storage contracts are not touched. Search timing is NOT
# duplicated here: ``eval_ms`` (column) and ``eval_details["engine_ms"]`` /
# ``["prefetch_hit"]`` are already written by the strategy/engine wrapper; the
# read side (``bot2048 db latency``) derives IPC = eval_ms - engine_ms.

#: ``eval_details["timing"]`` keys, in accumulator order (+ ``total_ms`` = sum).
_TIMING_KEYS: tuple[str, ...] = (
    "capture_ms",  # frame grabs (device.screenshot), summed over all captures
    "perceive_ms",  # board parses (read_board), summed over all parses
    "detect_ms",  # screen classification (detect_state), summed likewise
    "swipe_ms",  # humanized adb swipes (all attempts of the move)
    "settle_ms",  # deliberate waits for a settled frame (incl. predict-gate polls)
)
_CAPTURE, _PERCEIVE, _DETECT, _SWIPE, _SETTLE = range(len(_TIMING_KEYS))


class _PhaseSpan:
    """Reusable enter/exit span of one phase segment (no per-use allocation).

    NOT reentrant -- the live loop is strictly sequential, one phase at a time.
    """

    __slots__ = ("_index", "_t0", "_timer")

    def __init__(self, timer: _MovePhaseTimer) -> None:
        self._timer = timer
        self._index = 0
        self._t0 = 0.0

    def __enter__(self) -> _PhaseSpan:
        self._t0 = self._timer._clock()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # += : a phase is split into many segments per move (stabilization
        # re-captures, swipe retries, poll sleeps) -- segments accumulate.
        self._timer._ms[self._index] += (self._timer._clock() - self._t0) * 1000.0


class _MovePhaseTimer:
    """Accumulates the phase costs of ONE move in milliseconds.

    Deltas come from the runner's injected monotonic clock (``self._clock``,
    ``time.monotonic`` in production) -- never ``time.time()``. The object
    lives for the whole session: :meth:`reset` at the start of every move
    zeroes the fixed float slots in place, and the only allocation
    (:meth:`snapshot`) happens once per move at persist time, outside the
    hot capture/parse loop.
    """

    __slots__ = ("_clock", "_ms", "_span")

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._ms: list[float] = [0.0] * len(_TIMING_KEYS)
        self._span = _PhaseSpan(self)

    def reset(self) -> None:
        for index in range(len(_TIMING_KEYS)):
            self._ms[index] = 0.0

    def phase(self, index: int) -> _PhaseSpan:
        """``with timer.phase(_CAPTURE): ...`` -- allocation-free."""
        self._span._index = index
        return self._span

    def snapshot(self) -> dict[str, float]:
        """The ``eval_details["timing"]`` sub-dict (allocates; persist-time only)."""
        timing = {key: self._ms[index] for index, key in enumerate(_TIMING_KEYS)}
        timing["total_ms"] = sum(self._ms)
        return timing


def _device_capture_kind(device: object) -> str | None:
    """The FACTUAL frame source of the last ``screenshot()``: scrcpy/screencap.

    ``docs/research/telemetry_review.md`` M3: ``db latency`` used to read the
    CONFIGURED capture from ``experiments.params_json``, so a silent fallback
    from the scrcpy stream to ``screencap`` (~0.65 s/frame over WiFi) was
    invisible in p95. This reads the fact through the device's OPTIONAL public
    ``last_capture_kind()`` hook
    (:meth:`~bot2048.device.capture.StreamingCaptureDevice.last_capture_kind`
    provides it) -- the ``LiveDevice`` protocol does not require the method, so
    it is looked up by name, never by reaching into the capture layer's
    internals. A hook that raises or returns a non-string yields ``None``.

    Returns ``None`` for a device that cannot report it (a bare fake, or a
    plain ``AdbDevice`` used directly) so the read side keeps the config label.
    """
    reporter = getattr(device, "last_capture_kind", None)
    if not callable(reporter):
        return None
    try:
        value = reporter()
    except Exception:  # a telemetry probe must never break the move
        return None
    return value if isinstance(value, str) else None


class _MoveCounters:
    """Per-move reliability counters (docs/research/telemetry_review.md M1-M3).

    Sealed into ``eval_details["move"]`` at commit time next to the phase
    ``timing`` bucket (the frozen recording contract stays untouched). Like
    :class:`_MovePhaseTimer` the object lives for the whole session and is
    :meth:`reset` at the start of every move -- no per-move allocation until
    :meth:`snapshot` at persist time.

    Fields: ``capture_count`` (frames grabbed this move), ``swipe_attempts``
    (1 = registered first try), the predict-gate outcome (``gate_hit`` /
    ``gate_fallback`` / ``gate_frames`` -- the early-spawn metric #1, until now
    only in structlog), the FACTUAL frame source (``capture_kind``,
    ``screencap`` the moment ANY frame of the move fell back off the stream),
    and ``apk_age_ms`` -- the host mailbox dwell of the APK packet the move was
    verified from (``None`` when the move was not packet-gated or the source
    cannot measure it), the direct measure of the background-drain latency win.
    """

    __slots__ = (
        "apk_age_ms",
        "capture_count",
        "capture_kind_known",
        "capture_screencap",
        "gate_fallback",
        "gate_frames",
        "gate_hit",
        "swipe_attempts",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.capture_count = 0
        self.swipe_attempts = 0
        self.gate_hit = False
        self.gate_fallback = False
        self.gate_frames = 0
        self.capture_screencap = False
        self.capture_kind_known = False
        self.apk_age_ms: float | None = None

    def note_capture(self, kind: str | None) -> None:
        """Bill one captured frame plus its FACTUAL source (scrcpy/screencap)."""
        self.capture_count += 1
        if kind is not None:
            self.capture_kind_known = True
            if kind == "screencap":
                self.capture_screencap = True

    def snapshot(self) -> dict[str, Any]:
        """The ``eval_details["move"]`` sub-dict (allocates; persist-time only)."""
        move: dict[str, Any] = {
            "capture_count": self.capture_count,
            "swipe_attempts": self.swipe_attempts,
            "gate_hit": self.gate_hit,
            "gate_fallback": self.gate_fallback,
            "gate_frames": self.gate_frames,
        }
        if self.capture_kind_known:
            move["capture_kind"] = "screencap" if self.capture_screencap else "scrcpy"
        if self.apk_age_ms is not None:
            # Written only for packet-gated moves whose source measured the
            # mailbox dwell -- absent keys keep old rows / frame-path moves
            # byte-identical, and the read side (db latency) stays NULL-safe.
            move["apk_age_ms"] = self.apk_age_ms
        return move


class _TimedDevice:
    """:class:`LiveDevice` delegate that bills ``screenshot()`` to CAPTURE.

    Exactly one extra pair of clock reads per frame; behavior is unchanged, so
    ``escalation.probe_screen`` keeps working against the same device surface.
    It also tallies the frame into the move's :class:`_MoveCounters` (count +
    the FACTUAL scrcpy/screencap source) so a silent screencap fallback shows
    up per-move, not just in a log line.
    """

    __slots__ = ("_counters", "_device", "_timer")

    def __init__(
        self, device: LiveDevice, timer: _MovePhaseTimer, counters: _MoveCounters
    ) -> None:
        self._device = device
        self._timer = timer
        self._counters = counters

    def screenshot(self) -> Image:
        with self._timer.phase(_CAPTURE):
            frame = self._device.screenshot()
        self._counters.note_capture(_device_capture_kind(self._device))
        return frame

    def ui_dump(self) -> str:
        return self._device.ui_dump()

    def tap(self, x: int, y: int, *, sensitive: bool = False) -> None:
        self._device.tap(x, y, sensitive=sensitive)

    def wake(self) -> None:
        self._device.wake()

    def swipe_direction(
        self, direction: Direction, grid_bbox: BBox, humanizer: Humanizer
    ) -> SwipeParams:
        return self._device.swipe_direction(direction, grid_bbox, humanizer)


class _TimedDetector:
    """:class:`StateDetector` delegate that bills classification to DETECT."""

    __slots__ = ("_detect", "_timer")

    def __init__(self, detect: StateDetector, timer: _MovePhaseTimer) -> None:
        self._detect = detect
        self._timer = timer

    def __call__(self, image: Image) -> StateDetection:
        with self._timer.phase(_DETECT):
            return self._detect(image)


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of ``values`` (empty -> 0.0). Telemetry-grade."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((pct / 100.0) * (len(ordered) - 1))
    return ordered[max(0, min(len(ordered) - 1, index))]


@dataclass(slots=True)
class _GameTelemetry:
    """Per-game reliability accumulators (docs/research/telemetry_review.md N6).

    Summed move-by-move in :meth:`LiveRunner._commit_move`, then folded into
    the ``GAME_OVER`` ops ``details_json`` by :meth:`LiveRunner._end_game` --
    the game-level analog of the per-move ``eval_details["move"]`` bucket. Lets
    a 30-50 game campaign be compared with one ``SELECT`` (predict-gate hit%,
    vision fallbacks, swipe-retry rate, moves/s, first-1024 ply) without
    reconstructing anything from the ``moves`` table.
    """

    gate_moves: int = 0  # moves the predict-gate actually ran on
    gate_hits: int = 0  # of those, accepted on the fast path
    vision_fallback_moves: int = 0  # moves with >=1 color-fallback/suspect cell
    swipe_retry_total: int = 0  # extra swipes beyond the first, summed
    capture_screencap_moves: int = 0  # moves that fell back off the stream
    first_1024_ply: int | None = None  # ply the board first reached a 1024 tile
    first_2048_ply: int | None = None  # ply the board first reached 2048
    move_ms: list[float] = field(default_factory=list)  # per-move total_ms

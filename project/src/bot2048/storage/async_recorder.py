"""AsyncRecorder -- one background SQLite writer thread for the live loop.

Implements the frozen :class:`~bot2048.recording.Recorder` protocol by wrapping
another recorder (in production :class:`~bot2048.storage.sqlite_recorder.SqliteRecorder`,
unchanged) so that the per-move hot path only enqueues:

* **The writer thread owns the inner recorder.** It is *constructed* on the
  writer thread (pass a zero-arg factory), so the SQLite connection never
  leaves the thread that opened it (``check_same_thread``); the loop thread
  never touches sqlite, JSON serialisation (``SqliteRecorder.record``) or a
  WAL checkpoint again (design-strategy-logging §2 rows 3-4, P6/P12).
* **Small time-based batches.** The writer asks the inner recorder to
  ``flush()`` after ``flush_every_n`` events (64) or when the oldest unflushed
  event is ``flush_every_s`` old (50 ms), whichever comes first -- a shorter
  durability window than the inner recorder's own 256-event batch (a hard kill
  loses <= ~50 ms of moves instead of up to 128).
* **Never drop a game event** (owner decision Q4): ``GameStarted`` /
  ``MovePlayed`` / ``TileSpawned`` / ``GameEnded`` are the recorded game, the
  source of truth. When the bounded queue (20k) is full the caller *blocks*
  with backpressure; the stall is counted (:attr:`RecorderStats.backpressure`)
  and WARNed (``recorder.backpressure``) so a run where it ever fires is
  visible, never silent.
* **Failures surface where they always did.** A failing inner ``record`` /
  ``flush`` is retried by the writer with exponential backoff (50 ms .. 2 s);
  the first failure since the last report is re-raised as
  :class:`~bot2048.storage.errors.RecorderError` on the loop thread's *next*
  :meth:`record` call -- AFTER that call's event has been enqueued, so the
  existing ``STORAGE_ERROR`` WARN path in the runner fires and no event is lost
  to the report itself. A ``record`` failure is never re-sent: a buffering inner
  (``SqliteRecorder``) has already taken the row and keeps it across a failed
  flush, so re-recording would duplicate it; the retried ``flush`` carries it.
* :meth:`flush` enqueues a marker and returns (the game-end flush in the live
  runner must not stall the next game); :meth:`flush_sync` waits for the marker
  with a timeout; :meth:`close` flushes, closes the inner recorder *on the
  writer thread*, joins (bounded) and is idempotent.

Shutdown order in the session guard: ``recorder.close()`` -> ``ops.end_run``
-> ``tg.stop`` -> ``stop_logging``, so the run's rows are on disk before the
run row is closed. Sim keeps the plain ``SqliteRecorder`` (throughput-bound,
not latency-bound).
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Self

import structlog
from structlog.stdlib import get_logger

from bot2048.recording import GameEvent, Recorder
from bot2048.storage.errors import RecorderError
from bot2048.storage.sqlite_recorder import SqliteRecorder

log = get_logger(__name__)

DEFAULT_MAXSIZE: Final[int] = 20_000
"""Bounded queue depth; beyond it the loop thread blocks (never drops)."""

DEFAULT_FLUSH_EVERY_S: Final[float] = 0.05
"""Max age of the oldest unflushed event before the writer flushes (durability window)."""

DEFAULT_FLUSH_EVERY_N: Final[int] = 64
"""Max events handed to the inner recorder between two flushes."""

DEFAULT_CLOSE_TIMEOUT_S: Final[float] = 5.0
"""How long :meth:`AsyncRecorder.close` waits for the writer to drain and exit."""

_BACKOFF_BASE_S: Final[float] = 0.05
_BACKOFF_MAX_S: Final[float] = 2.0
_PUT_SLICE_S: Final[float] = 0.5  # blocking-put slice so a dead writer is noticed


def _log_debug(event: str, **kw: object) -> None:
    """Debug-log only when the application configured structlog (library rule)."""
    if structlog.is_configured():
        log.debug(event, **kw)


@dataclass(slots=True)
class _Marker:
    """Queue item that is not an event: flush (and optionally close) the inner."""

    done: threading.Event
    close: bool = False


@dataclass(frozen=True, slots=True)
class RecorderStats:
    """Counters of one :class:`AsyncRecorder` (loop-thread + writer-thread views)."""

    enqueued: int  # events accepted by record()
    written: int  # events the inner recorder accepted (record() returned)
    backpressure: int  # record() calls that had to block on a full queue
    failures: int  # inner record()/flush() exceptions (each retried with backoff)
    flush_markers_dropped: int  # flush() markers skipped because the queue was full
    depth: int  # current queue depth


class AsyncRecorder:
    """Recorder protocol implementation: enqueue here, persist on a writer thread.

    ``inner`` is either a zero-arg factory (**required** for ``SqliteRecorder``,
    whose connection must be created on the writer thread) or an already built
    thread-safe recorder (tests: ``ListRecorder``). The constructor starts the
    writer and waits until the inner recorder is open; an opening failure is
    re-raised here so startup semantics match the plain recorder ("the DB must
    work at start").
    """

    def __init__(
        self,
        inner: Recorder | Callable[[], Recorder],
        *,
        maxsize: int = DEFAULT_MAXSIZE,
        flush_every_s: float = DEFAULT_FLUSH_EVERY_S,
        flush_every_n: int = DEFAULT_FLUSH_EVERY_N,
        name: str = "bot2048-recorder",
        open_timeout_s: float = 60.0,
    ) -> None:
        if isinstance(inner, SqliteRecorder):
            raise TypeError(
                "pass a factory (lambda: SqliteRecorder(...)) so the SQLite connection "
                "is opened on the writer thread"
            )
        if isinstance(inner, Recorder):
            ready = inner
            self._factory: Callable[[], Recorder] = lambda: ready
        else:
            self._factory = inner
        self._q: queue.Queue[GameEvent | _Marker] = queue.Queue(maxsize=max(1, maxsize))
        self._maxsize = max(1, maxsize)
        self._flush_every_s = max(0.0, flush_every_s)
        self._flush_every_n = max(1, flush_every_n)
        # -- loop-thread state -------------------------------------------------------
        self._closed = False
        self._close_lock = threading.Lock()
        self._enqueued = 0
        self._backpressure = 0
        self._flush_markers_dropped = 0
        # -- writer-thread state (read by stats; ints, racy reads are fine) --------
        self._written = 0
        self._failures = 0
        self._backoff = _BACKOFF_BASE_S
        # -- cross-thread -----------------------------------------------------------
        self._error_lock = threading.Lock()
        self._error: BaseException | None = None  # first unreported inner failure
        self._open_error: BaseException | None = None
        self._opened = threading.Event()
        self._closing = threading.Event()  # cuts the writer's backoff sleep short
        self._writer = threading.Thread(target=self._run, name=name, daemon=True)
        self._writer.start()
        if not self._opened.wait(open_timeout_s):
            self._closed = True
            raise RecorderError(f"async recorder: writer did not open within {open_timeout_s}s")
        open_error = self._open_failure()
        if open_error is not None:
            self._closed = True
            if isinstance(open_error, RecorderError):
                raise open_error
            raise RecorderError(
                f"async recorder: inner recorder failed to open: {open_error}"
            ) from open_error
        _log_debug(
            "async_recorder_started",
            maxsize=self._maxsize,
            flush_every_s=self._flush_every_s,
            flush_every_n=self._flush_every_n,
        )

    # -- Recorder protocol (loop thread) -------------------------------------------

    def record(self, event: GameEvent) -> None:
        """Enqueue one game event; never drops it.

        Blocks with backpressure when the queue is full. Raises
        :class:`RecorderError` after closing, when the writer thread is gone, or
        -- AFTER the event was enqueued -- to report a writer failure that
        happened since the previous report.
        """
        if self._closed:
            raise RecorderError("async recorder is closed")
        if not self._writer.is_alive():
            raise RecorderError("async recorder: writer thread is not running; event not persisted")
        self._put_event(event)
        self._enqueued += 1
        error = self._take_error()
        if error is not None:
            raise RecorderError(
                f"async recorder: background write failed ({self._failures} failures so far, "
                f"queue depth {self._q.qsize()}): {error}"
            ) from error

    def flush(self) -> None:
        """Ask the writer to flush the inner recorder; does NOT wait.

        A full queue skips the marker (counted): the writer is then far behind
        and its own count/age batching flushes the backlog anyway.
        """
        if self._closed:
            return
        try:
            self._q.put_nowait(_Marker(done=threading.Event()))
        except queue.Full:
            self._flush_markers_dropped += 1
            _log_debug("async_recorder_flush_marker_dropped", depth=self._q.qsize())

    def flush_sync(self, timeout: float = DEFAULT_CLOSE_TIMEOUT_S) -> bool:
        """Flush and wait until everything enqueued so far is persisted.

        Returns ``False`` when the marker could not be enqueued or processed
        within ``timeout`` (writer stuck or far behind). ``True`` after close:
        closing already flushed and nothing can be pending.
        """
        if self._closed:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        marker = _Marker(done=threading.Event())
        try:
            self._q.put(marker, timeout=max(0.0, timeout))
        except queue.Full:
            return False
        return marker.done.wait(max(0.0, deadline - time.monotonic()))

    def close(self, timeout: float = DEFAULT_CLOSE_TIMEOUT_S) -> None:
        """Flush, close the inner recorder on the writer thread, join. Idempotent.

        Bounded by ``timeout``: a writer that cannot drain in time is left to
        finish as a daemon and the remaining depth is WARNed (``recorder.close_timeout``).
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._closing.set()
        if self._open_failure() is not None or not self._writer.is_alive():
            return
        deadline = time.monotonic() + max(0.0, timeout)
        marker = _Marker(done=threading.Event(), close=True)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0 or not self._writer.is_alive():
                break
            try:
                self._q.put(marker, timeout=min(_PUT_SLICE_S, remaining))
                break
            except queue.Full:
                continue
        self._writer.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._writer.is_alive():
            log.warning(
                "recorder.close_timeout",
                timeout_s=timeout,
                pending=self._q.qsize(),
                backpressure=self._backpressure,
                failures=self._failures,
            )
            return
        _log_debug(
            "async_recorder_closed",
            enqueued=self._enqueued,
            written=self._written,
            backpressure=self._backpressure,
            failures=self._failures,
        )

    # -- introspection ----------------------------------------------------------------

    @property
    def stats(self) -> RecorderStats:
        """Current counters (a consistent snapshot is not guaranteed across threads)."""
        return RecorderStats(
            enqueued=self._enqueued,
            written=self._written,
            backpressure=self._backpressure,
            failures=self._failures,
            flush_markers_dropped=self._flush_markers_dropped,
            depth=self._q.qsize(),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- loop-thread helpers --------------------------------------------------------------

    def _put_event(self, event: GameEvent) -> None:
        try:
            self._q.put_nowait(event)
            return
        except queue.Full:
            pass
        # Pathological: the writer is > maxsize events behind. Never drop a game
        # event -- block, but count it and say so (visible in the JSONL log and
        # in ``stats``), and notice a dead writer instead of waiting forever.
        self._backpressure += 1
        log.warning(
            "recorder.backpressure",
            depth=self._q.qsize(),
            maxsize=self._maxsize,
            count=self._backpressure,
            event_type=type(event).__name__,
        )
        while True:
            if not self._writer.is_alive():
                raise RecorderError(
                    "async recorder: writer thread died while the queue was full; "
                    f"{type(event).__name__} not persisted"
                )
            try:
                self._q.put(event, timeout=_PUT_SLICE_S)
                return
            except queue.Full:
                continue

    def _open_failure(self) -> BaseException | None:
        """The writer's opening exception (set by the writer thread), if any."""
        return self._open_error

    def _take_error(self) -> BaseException | None:
        if self._error is None:  # cheap unlocked fast path (hot path)
            return None
        with self._error_lock:
            error, self._error = self._error, None
        return error

    # -- writer thread ----------------------------------------------------------------------

    def _run(self) -> None:
        try:
            inner = self._factory()
        except BaseException as exc:
            self._open_error = exc
            self._opened.set()
            return
        self._opened.set()
        try:
            self._serve(inner)
        except BaseException as exc:  # defensive: the loop above never lets one out
            self._failures += 1
            with self._error_lock:
                if self._error is None:
                    self._error = exc
            log.error("recorder.writer_crashed", error=str(exc), error_type=type(exc).__name__)

    def _serve(self, inner: Recorder) -> None:
        pending = 0  # events handed to inner since its last successful flush
        deadline = 0.0  # age-based flush deadline (valid when pending > 0)
        while True:
            # Once close() has been requested, drain whatever is queued without
            # blocking and exit on empty -- even if the close marker itself never
            # fitted into a full queue (close() timed out), the inner recorder
            # still gets flushed and closed as soon as the backlog is gone.
            if self._closing.is_set():
                timeout: float | None = 0.0
            elif pending == 0:
                timeout = None
            else:
                timeout = max(0.0, deadline - time.monotonic())
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                pending = self._flush(inner, pending)
                if self._closing.is_set():
                    self._close_inner(inner)
                    return
                continue
            if isinstance(item, _Marker):
                pending = self._flush(inner, pending)
                if item.close:
                    self._close_inner(inner)
                    item.done.set()
                    return
                item.done.set()
                continue
            if not self._write(inner, item):
                continue
            if pending == 0:
                deadline = time.monotonic() + self._flush_every_s
            pending += 1
            if pending >= self._flush_every_n:
                pending = self._flush(inner, pending)

    def _write(self, inner: Recorder, event: GameEvent) -> bool:
        try:
            inner.record(event)
        except Exception as exc:
            self._fail(exc, what=f"record({type(event).__name__})")
            return False
        self._written += 1
        self._backoff = _BACKOFF_BASE_S
        return True

    def _flush(self, inner: Recorder, pending: int) -> int:
        """Flush the inner recorder; returns the still-pending count (0 on success)."""
        if pending == 0:
            return 0
        try:
            inner.flush()
        except Exception as exc:
            self._fail(exc, what="flush")
            return pending  # the inner keeps its buffer; the next flush retries it
        self._backoff = _BACKOFF_BASE_S
        return 0

    def _close_inner(self, inner: Recorder) -> None:
        try:
            inner.close()
        except Exception as exc:
            self._failures += 1
            log.warning("recorder.inner_close_failed", error=str(exc))

    def _fail(self, exc: BaseException, *, what: str) -> None:
        """Count + surface one inner failure, then back off (interruptible by close)."""
        self._failures += 1
        with self._error_lock:
            if self._error is None:
                self._error = exc
        log.warning(
            "recorder.write_failed",
            what=what,
            error=str(exc),
            failures=self._failures,
            backoff_s=self._backoff,
            depth=self._q.qsize(),
        )
        if not self._closing.is_set():
            self._closing.wait(self._backoff)
        self._backoff = min(self._backoff * 2.0, _BACKOFF_MAX_S)

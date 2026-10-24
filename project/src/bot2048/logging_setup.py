"""structlog configuration: pretty console + JSONL file, with game context helpers.

Library modules obtain loggers via ``structlog.get_logger(__name__)`` and never
``print``; CLI output goes through rich. Every log record carries whatever
game_id/ply context is currently bound via :func:`bind_game_context`.

Two properties matter on the live per-move hot path (docs/ops-monitoring.md
§2.1, briefs/live-pipeline-timing.md):

* **Level gating is cheap.** ``structlog.stdlib.filter_by_level`` is the FIRST
  processor, so a ``log.debug`` emitted at INFO level is dropped by a single
  ``isEnabledFor`` check BEFORE any timestamp/context/render work -- the
  high-frequency per-move debug lines (``adb.swipe`` / ``live.apk_gate_hit`` /
  ``live.predict_gate_hit`` / ``vision.cell.color_fallback``) cost almost
  nothing at the default run level, yet come back in full with ``--verbose``.
* **Serialization + I/O leave the hot path.** The JSONL render and the file
  write run on a background :class:`~logging.handlers.QueueListener` thread; the
  move loop only enqueues a record. :func:`stop_logging` (registered via
  ``atexit`` and called from the session guard) drains the queue on shutdown so
  no line is lost on a clean exit or a FATAL.
* **The queue is bounded** (design-strategy-logging §5.5, P11). A stalled
  console consumer (Windows QuickEdit "mark" freezing stdout, a slow
  ``tail | grep``) used to grow an unbounded queue without limit and make
  ``stop_logging`` drain forever. Now the queue holds at most
  :data:`LOG_QUEUE_MAXSIZE` records; on ``Full`` a DEBUG/INFO record is dropped
  and counted, a WARNING+ record waits up to :data:`WARN_BLOCK_S` for room and
  is dropped (counted) only then. The listener reports the count as ONE
  ``logging.dropped n=...`` WARNING once the queue has room again, and
  :func:`stop_logging` gives up draining after its timeout and returns how many
  records were left behind instead of hanging.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import queue
import sys
import threading
import time
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Final

import structlog
from structlog.typing import Processor

_DEFAULT_LOG_DIR: Final[Path] = Path("logs")

_MAX_JSONL_BYTES: Final[int] = 50 * 1024 * 1024
"""Size cap per daily JSONL file (docs/ops-monitoring.md §2.1); on overflow
the current file is rotated to ``bot2048-YYYYMMDD-1.jsonl``, ``-2``, ..."""

LOG_QUEUE_MAXSIZE: Final[int] = 50_000
"""Bounded depth of the async log queue; beyond it DEBUG/INFO records are dropped
(counted) and WARNING+ records wait :data:`WARN_BLOCK_S` for room first."""

WARN_BLOCK_S: Final[float] = 0.010
"""How long a WARNING+ record may block the emitting thread on a full queue."""

STOP_TIMEOUT_S: Final[float] = 10.0
"""Default drain budget of :func:`stop_logging`; past it the remainder is abandoned."""

#: The queue carries LogRecords plus the listener's ``None`` stop sentinel.
_LogQueue = queue.Queue[logging.LogRecord | None]

#: The async log pump: a background QueueListener drains records off the
#: per-move critical path into the (slow) JSONL file + console. Module-global so
#: a repeat :func:`setup_logging` can tear the previous one down and
#: :func:`stop_logging` (atexit / session-guard finally) can flush every
#: buffered line on shutdown.
_listener: _BoundedQueueListener | None = None


class _DropCounter:
    """Thread-safe count of records dropped on a full queue (reset on report)."""

    __slots__ = ("_lock", "_n")

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def add(self, n: int = 1) -> None:
        with self._lock:
            self._n += n

    def peek(self) -> int:
        return self._n  # racy by design: a cheap "anything to report?" probe

    def take(self) -> int:
        with self._lock:
            n, self._n = self._n, 0
        return n


def _rotated_name(default_name: str) -> str:
    """``logs/bot2048-20260710.jsonl.3`` -> ``logs/bot2048-20260710-3.jsonl``."""
    base, _, index = default_name.rpartition(".")
    if index.isdigit() and base.endswith(".jsonl"):
        return f"{base[: -len('.jsonl')]}-{index}.jsonl"
    return default_name


class _NonFormattingQueueHandler(QueueHandler):
    """Enqueue the RAW :class:`~logging.LogRecord`, formatting nothing.

    :meth:`logging.handlers.QueueHandler.prepare` stringifies the record via
    ``self.format()`` on the CALLING (hot-path) thread and clears
    ``msg``/``args``/``exc_info``. That both defeats async logging (the render
    still happens inline) and destroys structlog's ``ProcessorFormatter``
    contract, which needs the original event-dict in ``record.msg``. We enqueue
    the record untouched and let the listener's handlers render it on the
    background thread. Safe because the queue is an in-process
    :class:`queue.Queue` (no pickling, so keeping ``exc_info`` is fine) and the
    record is not mutated before the listener drains it.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record


class _BoundedQueueHandler(_NonFormattingQueueHandler):
    """Drop-on-Full policy for the bounded queue (design-strategy-logging §5.5).

    DEBUG/INFO: dropped immediately and counted -- the per-move cadence line is
    INFO, and losing a few of them under a frozen console beats stalling the
    move loop or growing memory without bound. WARNING+: wait up to
    :data:`WARN_BLOCK_S` for room (incident lines are worth 10 ms), then drop and
    count too, so a dead consumer can never wedge the emitting thread.
    """

    def __init__(self, log_queue: _LogQueue, dropped: _DropCounter) -> None:
        super().__init__(log_queue)
        self._q = log_queue
        self._dropped = dropped

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self._q.put_nowait(record)
            return
        except queue.Full:
            pass
        if record.levelno >= logging.WARNING:
            try:
                self._q.put(record, timeout=WARN_BLOCK_S)
                return
            except queue.Full:
                pass
        self._dropped.add()


class _BoundedQueueListener(QueueListener):
    """Listener that reports drops and whose :meth:`stop` is bounded in time.

    After every handled record it checks the drop counter; when records were
    lost and the queue has room again (below half) it emits ONE
    ``logging.dropped n=<count>`` WARNING through the normal structlog path, so
    the gap is visible in both sinks exactly where it happened.
    """

    def __init__(
        self,
        log_queue: _LogQueue,
        *handlers: logging.Handler,
        dropped: _DropCounter,
        maxsize: int,
        respect_handler_level: bool = False,
    ) -> None:
        super().__init__(log_queue, *handlers, respect_handler_level=respect_handler_level)
        self._q = log_queue
        self._dropped = dropped
        self._maxsize = maxsize
        self._room = max(1, maxsize // 2)

    def handle(self, record: logging.LogRecord) -> None:
        super().handle(record)
        if self._dropped.peek() and self._q.qsize() < self._room:
            self._report_dropped()

    def _report_dropped(self) -> None:
        n = self._dropped.take()
        if n:
            structlog.get_logger(__name__).warning("logging.dropped", n=n, maxsize=self._maxsize)

    def stop_within(self, timeout: float) -> int:
        """Drain for at most ``timeout`` seconds; return the records left behind.

        The stdlib :meth:`stop` does ``put_nowait(sentinel)`` + an unbounded
        ``join`` -- on a full queue behind a frozen console that raises
        ``queue.Full`` or hangs forever. Here the sentinel is offered with a
        timeout, the join is bounded, and a thread still alive afterwards is
        left as the daemon it is (it keeps draining if the consumer ever wakes).
        """
        thread = self._thread
        if thread is None:
            return 0
        deadline = time.monotonic() + max(0.0, timeout)
        self._report_dropped()  # last chance to say how much was lost
        # ``None`` is QueueListener._sentinel: _monitor exits on it. A stuck
        # consumer keeps the queue full -> Full; the bounded join below is then
        # what limits us.
        with contextlib.suppress(queue.Full):
            self._q.put(None, timeout=max(0.0, min(timeout, 1.0)))
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            return self._q.qsize()
        self._thread = None
        return 0


def _stop_listener(timeout: float = STOP_TIMEOUT_S) -> int:
    """Drain + join the async pump if one is running (idempotent); records left."""
    global _listener
    if _listener is None:
        return 0
    listener, _listener = _listener, None
    return listener.stop_within(timeout)


def stop_logging(timeout: float = STOP_TIMEOUT_S) -> int:
    """Flush + stop the async log pump so no buffered line is lost on shutdown.

    Registered via ``atexit`` by :func:`setup_logging` and also safe to call
    explicitly from the session guard's ``finally`` (idempotent). After it runs
    the root logger still enqueues records, but nothing drains them until the
    next :func:`setup_logging`; call it only when the process is winding down.

    Draining is bounded by ``timeout``: a consumer that is still stuck when it
    expires is abandoned and the number of records left in the queue is
    returned (``0`` on a clean drain) so the caller can say so on stderr.
    """
    return _stop_listener(timeout)


def setup_logging(
    json_logs: bool = True,
    log_dir: Path = _DEFAULT_LOG_DIR,
    *,
    level: int | str = logging.INFO,
    queue_maxsize: int = LOG_QUEUE_MAXSIZE,
) -> Path | None:
    """Configure structlog + stdlib logging for the whole process.

    Always attaches a pretty console renderer (to ``stdout``, matching the
    unconfigured-structlog default so a launcher redirect keeps working). When
    ``json_logs`` is true, additionally writes machine-readable JSONL to
    ``log_dir/bot2048-YYYYMMDD.jsonl`` and returns that path.

    ``level`` gates emission: at the default ``INFO`` the per-move debug lines
    are dropped cheaply (``--verbose`` -> ``DEBUG`` brings them back). Both
    sinks run behind a background queue bounded at ``queue_maxsize`` records
    (see the module docstring for the drop policy), so the caller (or
    ``atexit``) MUST :func:`stop_logging` on shutdown to flush.

    Safe to call repeatedly (e.g. in tests) -- the previous async pump and
    handlers are torn down and replaced.
    """
    shared: list[Processor] = [
        # Cheap short-circuit FIRST: a gated log.debug() on the per-move hot
        # path is dropped here (isEnabledFor) before any timestamp/context/
        # render work is done.
        structlog.stdlib.filter_by_level,
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    _stop_listener()  # tear down any previous async pump before re-handlering
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    # Real sinks: these run on the background listener thread, OFF the hot path.
    sinks: list[logging.Handler] = []

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                # Colors only on a real terminal: a live-run launcher redirects
                # stdout to the golden-log FILE, where ANSI escapes split field
                # values (total_ms=344.0 -> \x1b[36mtotal_ms\x1b[0m=...) and
                # break downstream grep/awk parsing. Mirrors unconfigured
                # structlog, which disables colors when stdout is not a TTY.
                structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
            ],
            foreign_pre_chain=shared,
        )
    )
    sinks.append(console_handler)

    jsonl_path: Path | None = None
    if json_logs:
        log_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = log_dir / f"bot2048-{datetime.now():%Y%m%d}.jsonl"
        file_handler = RotatingFileHandler(
            jsonl_path,
            maxBytes=_MAX_JSONL_BYTES,
            backupCount=99,
            encoding="utf-8",
        )
        file_handler.namer = _rotated_name
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    # show_locals=False is deliberate: the default is True, and a
                    # traceback logged from inside the Telegram transport would
                    # then serialise its local `url` -- which carries the bot
                    # token in the path -- straight into logs/*.jsonl.
                    structlog.processors.ExceptionRenderer(
                        structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
                    ),
                    structlog.processors.JSONRenderer(),
                ],
                foreign_pre_chain=shared,
            )
        )
        sinks.append(file_handler)

    # Async pump: the hot path only enqueues (QueueHandler.emit -> put_nowait on
    # a BOUNDED queue, so a move never blocks on the slow JSONL write and a
    # frozen console cannot grow memory without limit); the listener thread
    # does the ProcessorFormatter render + file I/O. A fatal still flushes via
    # stop_logging (atexit + the session guard's finally), bounded in time.
    global _listener
    dropped = _DropCounter()
    maxsize = max(1, queue_maxsize)
    log_queue: _LogQueue = queue.Queue(maxsize=maxsize)
    _listener = _BoundedQueueListener(
        log_queue, *sinks, dropped=dropped, maxsize=maxsize, respect_handler_level=True
    )
    _listener.start()
    atexit.register(_stop_listener)

    root.addHandler(_BoundedQueueHandler(log_queue, dropped))
    root.setLevel(level)
    return jsonl_path


def bind_game_context(game_id: str, ply: int | None = None) -> None:
    """Bind game_id (and optionally ply) to all subsequent log records."""
    if ply is None:
        structlog.contextvars.bind_contextvars(game_id=game_id)
    else:
        structlog.contextvars.bind_contextvars(game_id=game_id, ply=ply)


def bind_ply(ply: int) -> None:
    """Update only the bound ply (game_id stays bound)."""
    structlog.contextvars.bind_contextvars(ply=ply)


def clear_game_context() -> None:
    """Remove game_id/ply from the logging context (end of game)."""
    structlog.contextvars.unbind_contextvars("game_id", "ply")

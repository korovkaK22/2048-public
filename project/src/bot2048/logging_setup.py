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
"""

from __future__ import annotations

import atexit
import logging
import queue
import sys
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

#: The async log pump: a background QueueListener drains records off the
#: per-move critical path into the (slow) JSONL file + console. Module-global so
#: a repeat :func:`setup_logging` can tear the previous one down and
#: :func:`stop_logging` (atexit / session-guard finally) can flush every
#: buffered line on shutdown.
_listener: QueueListener | None = None


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


def _stop_listener() -> None:
    """Drain + join the async pump if one is running (idempotent)."""
    global _listener
    if _listener is not None:
        _listener.stop()  # enqueues a sentinel, drains the queue, joins the thread
        _listener = None


def stop_logging() -> None:
    """Flush + stop the async log pump so no buffered line is lost on shutdown.

    Registered via ``atexit`` by :func:`setup_logging` and also safe to call
    explicitly from the session guard's ``finally`` (idempotent). After it runs
    the root logger still enqueues records, but nothing drains them until the
    next :func:`setup_logging`; call it only when the process is winding down.
    """
    _stop_listener()


def setup_logging(
    json_logs: bool = True,
    log_dir: Path = _DEFAULT_LOG_DIR,
    *,
    level: int | str = logging.INFO,
) -> Path | None:
    """Configure structlog + stdlib logging for the whole process.

    Always attaches a pretty console renderer (to ``stdout``, matching the
    unconfigured-structlog default so a launcher redirect keeps working). When
    ``json_logs`` is true, additionally writes machine-readable JSONL to
    ``log_dir/bot2048-YYYYMMDD.jsonl`` and returns that path.

    ``level`` gates emission: at the default ``INFO`` the per-move debug lines
    are dropped cheaply (``--verbose`` -> ``DEBUG`` brings them back). Both
    sinks run behind a background queue, so the caller (or ``atexit``) MUST
    :func:`stop_logging` on shutdown to flush.

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

    # Async pump: the hot path only enqueues (QueueHandler.emit -> queue.put on
    # an unbounded queue, so a move never blocks on the slow JSONL write); the
    # listener thread does the ProcessorFormatter render + file I/O. A fatal
    # still flushes via stop_logging (atexit + the session guard's finally).
    global _listener
    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
    _listener = QueueListener(log_queue, *sinks, respect_handler_level=True)
    _listener.start()
    atexit.register(_stop_listener)

    root.addHandler(_NonFormattingQueueHandler(log_queue))
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

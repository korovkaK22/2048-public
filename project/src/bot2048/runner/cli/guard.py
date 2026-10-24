"""The lifecycle guard every live command runs under (``_run_guarded``)."""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from bot2048.device.adb import AdbDevice
from bot2048.device.capture import StreamingCaptureDevice
from bot2048.device.supervisor import Supervisor
from bot2048.logging_setup import setup_logging, stop_logging
from bot2048.runner.cli.common import _BuildFn, _DetectFn, err_console
from bot2048.runner.lifecycle import LockHeldError, RestartLoopError, run_session
from bot2048.storage.sqlite_recorder import ExperimentMeta, SqliteRecorder
from bot2048.tgnotify import get_notifier


def _run_guarded(
    *,
    device: AdbDevice | StreamingCaptureDevice,
    detect: _DetectFn,
    db: Path,
    args: dict[str, object],
    build: _BuildFn,
    experiment: str,
    strategy_name: str,
    params: dict[str, object],
    verbose: bool = False,
) -> None:
    """run_session + Telegram bracket + Supervisor/Recorder wiring (§9.8).

    On FatalIncident run_session raises SystemExit(2) with the bundle/alert
    already on disk; the Telegram bracket still closes in ``finally``.
    typer.Exit is never raised inside the session (it would count as CRASH).

    Logging is configured HERE (not in ``play``/``research``) so the CLI unit
    tests -- which stub this function -- never start the async log pump. The
    per-move hot-path debug lines are gated off unless ``verbose`` selects
    DEBUG; :func:`stop_logging` in the ``finally`` flushes the background queue
    so a FATAL loses nothing.
    """
    setup_logging(json_logs=True, level=logging.DEBUG if verbose else logging.INFO)
    tg = get_notifier()
    tg.start()
    ok = False
    try:
        with run_session(db_path=db, args=dict(args)) as ctx:
            ctx.alerts.notifier = tg.notify_alert  # ERROR/FATAL -> Telegram (§3.2)
            tg.session_start(ctx.run_id, strategy_name)
            supervisor = Supervisor(
                device,
                detect,
                ctx.alerts,
                run_id=ctx.run_id,
                ops=ctx.ops,
                ring=ctx.ring,
            )
            recorder = SqliteRecorder(
                db,
                ExperimentMeta(
                    experiment_id=experiment,
                    kind="live",
                    strategy=strategy_name,
                    params=dict(params),
                ),
            )
            try:
                runner = build(ctx, recorder, supervisor, tg.notify)
                runner.run()
            finally:
                recorder.close()
        ok = True
    except (LockHeldError, RestartLoopError) as exc:
        err_console.print(f"[red]refusing to start:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    finally:
        tg.session_end(ok, reason="" if ok else "див. logs/alerts.jsonl")
        tg.stop(flush_timeout=5.0)
        stop_logging()  # drain the async log queue so no line is lost on exit

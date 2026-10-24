"""Shared plumbing for the ``bot2048 live`` sub-app.

Consoles, the logger, the small structural protocols the commands wire their
runners through, and the one device seam every command uses
(:func:`_connect`). Nothing here touches the phone at import time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import structlog
import typer
from rich.console import Console

from bot2048.core.types import Board
from bot2048.device.adb import AdbDevice, DeviceError
from bot2048.device.config import DeviceConfig
from bot2048.device.supervisor import Supervisor
from bot2048.recording import Recorder
from bot2048.runner.incidents import Image
from bot2048.runner.lifecycle import RunContext
from bot2048.runner.live import NotifyHook
from bot2048.vision.states import StateDetection

console = Console()
err_console = Console(stderr=True)

#: One logger for the whole sub-app, named after the PACKAGE
#: (``bot2048.runner.cli``) rather than each submodule, so the ``logger`` field
#: in ``logs/*.jsonl`` reads exactly as it did before the package split and
#: stays greppable across old and new runs.
log = structlog.get_logger(__package__)


class _DetectFn(Protocol):
    def __call__(self, image: Image) -> StateDetection: ...


class _ReadFn(Protocol):
    def __call__(self, image: Image) -> Board: ...


class _Runner(Protocol):
    def run(self) -> None: ...


# The game-end notification attaches an image, so the hook accepts an optional
# photo path (see live.NotifyHook); ``tg.notify`` already supports it.
_NotifyFn = NotifyHook
# The guard hands the runner the frozen Recorder PROTOCOL (live = AsyncRecorder
# around a SqliteRecorder), so the build closures must not depend on the
# concrete class.
_BuildFn = Callable[[RunContext, Recorder, Supervisor, _NotifyFn], _Runner]


def _connect(serial: str | None) -> AdbDevice:
    device = AdbDevice(DeviceConfig(serial=serial))
    try:
        device.connect()
    except DeviceError as exc:
        err_console.print(f"[red]device error:[/red] {exc}")
        err_console.print("check `adb devices` / docs/live-setup.md")
        raise typer.Exit(code=1) from exc
    return device

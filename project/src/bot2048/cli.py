"""bot2048 root CLI.

Each domain module owns its own typer sub-app:
    sim/cli.py      -> app  (registered as "sim")
    storage/cli.py  -> app  (registered as "db")
    vision/cli.py   -> app  (registered as "vision")
    runner/cli.py   -> app  (registered as "live")

Sub-apps are registered lazily so a missing/broken module never blocks the
others (important while modules are developed in parallel).
"""

from __future__ import annotations

import importlib

import typer

app = typer.Typer(
    name="bot2048", no_args_is_help=True, help="2048 bot: sim lab + Android live player."
)

_SUBAPPS: tuple[tuple[str, str], ...] = (
    ("sim", "bot2048.sim.cli"),
    ("db", "bot2048.storage.cli"),
    ("vision", "bot2048.vision.cli"),
    ("live", "bot2048.runner.cli"),
)

for _name, _module in _SUBAPPS:
    try:
        _mod = importlib.import_module(_module)
        app.add_typer(_mod.app, name=_name)
    except ImportError:  # module not implemented yet
        continue


if __name__ == "__main__":
    app()

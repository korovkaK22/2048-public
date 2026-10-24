"""``bot2048 live`` sub-app: live play and spawn research on a real phone.

Commands (host only, never Docker; see docs/live-setup.md):

* ``bot2048 live play``     -- the real loop under the full lifecycle guard
  (run_id + lock + restart budget + heartbeat + Telegram bridge).
* ``bot2048 live research`` -- spawn-rule measurement (cheap policy, many
  games), same guard.
* ``bot2048 live once``     -- DRY debug run: one capture -> state ->
  (parse -> decide), prints the decision, NEVER swipes or taps.

The concrete vision implementation (``detect_state`` / ``parse_board``) is
bound to the calibration in :mod:`.vision_bind` and injected into the runner
behind its local :class:`~bot2048.runner.live.StateDetector` / ``BoardReader``
protocols -- the loop itself never imports the parser, so vision/ can keep
moving.

Package layout -- one module per concern, so parallel live features stop
colliding in a single thousand-line file:

* :mod:`.common`      -- consoles, logger, runner protocols, ``_connect``.
* :mod:`.tools`       -- host binary/jar resolution (adb, ffmpeg, scrcpy-server).
* :mod:`.vision_bind` -- calibration-bound vision (``_Vision`` / ``_make_vision``).
* :mod:`.strategy`    -- ``--strategy`` name -> :class:`Strategy` factory.
* :mod:`.capture`     -- ``--capture`` kind -> capture device / board source.
* :mod:`.guard`       -- ``_run_guarded``: run_session + Telegram + Supervisor wiring.
* :mod:`.cmd_play` / :mod:`.cmd_research` / :mod:`.cmd_once` -- the commands.

Tests that monkeypatch a seam must patch the OWNING module (e.g.
``cmd_play._run_guarded``, ``tools._SCRCPY_TOOLS_DIR``): the few names
re-exported below are plain references, so patching them on this package
changes nothing at call time. Seams are deliberately NOT re-exported so a
stale ``monkeypatch.setattr(cli, "_connect", ...)`` fails loudly instead of
silently no-oping.
"""

from __future__ import annotations

import typer

from bot2048.runner.cli import cmd_once, cmd_play, cmd_research
from bot2048.runner.cli.capture import _make_capture_device
from bot2048.runner.cli.strategy import _make_strategy

app = typer.Typer(
    name="live",
    no_args_is_help=True,
    help="Live Android player (host only, never Docker).",
)

# Explicit registration (rather than ``@app.command()`` inside each module)
# keeps the command names and their order visible in one place and avoids
# importing modules for their side effects.
app.command(name="play")(cmd_play.play)
app.command(name="research")(cmd_research.research)
app.command(name="once")(cmd_once.once)

__all__ = ["_make_capture_device", "_make_strategy", "app"]

"""``python -m bot2048.tgnotify`` entrypoint (see :mod:`bot2048.tgnotify.cli`)."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

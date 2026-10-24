"""CLI: ``python -m bot2048.tgnotify {watch|bootstrap|send}``."""

from __future__ import annotations

from pathlib import Path

from bot2048 import tgnotify as _pkg

from .bootstrap import bootstrap
from .config import DEFAULT_ENV_PATH, TgConfig
from .formatting import _Item
from .notifier import TgNotifier
from .transport import TgError
from .watch import watch


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="bot2048.tgnotify", description=(_pkg.__doc__ or "").splitlines()[0]
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch", help="резервний вотчер alerts.jsonl + heartbeat")
    w.add_argument("--root", default=".", help="корінь repo (де logs/ і .secrets/)")
    b = sub.add_parser("bootstrap", help="одноразове визначення TG_CHAT_ID")
    b.add_argument("--root", default=".")
    s = sub.add_parser("send", help='ручна відправка: send "text" [photo]')
    s.add_argument("text")
    s.add_argument("photo", nargs="?", default=None)
    s.add_argument("--root", default=".")
    args = parser.parse_args(argv)

    if args.cmd == "watch":
        watch(args.root)
        return 0
    if args.cmd == "bootstrap":
        return bootstrap(args.root)
    # send
    cfg = TgConfig.from_env(Path(args.root) / DEFAULT_ENV_PATH)
    if not cfg.usable:
        print("tg не налаштовано (.secrets/telegram.env)")
        return 1
    notifier = TgNotifier(cfg, root=args.root)
    try:
        notifier._send_once(_Item("raw", "INFO", "MANUAL", args.text, photo_path=args.photo))
    except TgError as exc:
        print(f"збій відправки: {type(exc).__name__}")
        return 1
    print("відправлено")
    return 0

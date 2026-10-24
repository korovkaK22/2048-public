#!/usr/bin/env bash
# Manual / scripted Telegram send (docs/telegram-notify.md §6.3) -- a thin
# wrapper over `python -m bot2048.tgnotify send`, which owns the transport,
# the outbox and the token hygiene (token lives only inside TgConfig, never in
# argv, never printed; errors are reported without the URL).
#
#   scripts/tg_send.sh "текст" [шлях_до_фото]
#
# Secrets: ../.secrets/telegram.env (TG_BOT_TOKEN, TG_CHAT_ID), found by the
# module's own walk-up from project/. Exit 0 -- sent, 1 -- failure/no config.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 \"text\" [photo]" >&2
    exit 2
fi

# shellcheck source=_host_env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_host_env.sh"

exec "$PY" -m bot2048.tgnotify send "$@" --root .

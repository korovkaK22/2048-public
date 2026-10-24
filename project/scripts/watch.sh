#!/usr/bin/env bash
# Telegram watcher launcher: `python -m bot2048.tgnotify watch` from project/.
#
# The watcher is the RESERVE alert path (docs/telegram-notify.md §6): it tails
# logs/alerts.jsonl + logs/heartbeat.json and shouts when the runner itself is
# dead or hung. It must therefore run as a SEPARATE long-lived process (launchd
# KeepAlive on macOS -- scripts/macos/com.bot2048.watch.plist.template).
#
# Usage:  scripts/watch.sh            # extra args are passed through
#
# Secrets: tgnotify reads ../.secrets/telegram.env itself (walk-up from cwd,
# env vars are only a fallback), so sourcing it here is belt-and-braces for a
# launchd job whose cwd resolution differs from an interactive shell.
set -euo pipefail

# shellcheck source=_host_env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_host_env.sh"

source_secrets "../.secrets/telegram.env"       # TG_BOT_TOKEN / TG_CHAT_ID (never printed)

# --root . = project/: logs/ and control/ are cwd-relative here; .secrets/ is
# found one level up by tgnotify's own resolver.
exec "$PY" -m bot2048.tgnotify watch --root . "$@"

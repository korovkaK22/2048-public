#!/usr/bin/env bash
# Render the launchd templates for THIS checkout/user and (on macOS) load them
# as LaunchAgents in ~/Library/LaunchAgents.
#
# Usage:
#   scripts/macos/install.sh                 # render + bootstrap both agents
#   scripts/macos/install.sh --uninstall     # bootout + remove the plists
#   scripts/macos/install.sh --render DIR    # only write the rendered plists
#                                            # into DIR (any OS; used by CI/tests)
#
# Placeholders: @REPO@ = repo root (parent of project/), @HOME@ = $HOME.
# The watcher (KeepAlive) starts immediately; the runner is loaded but NOT
# started -- a human kicks it:  launchctl kickstart -k gui/$UID/com.bot2048.live
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
labels=(com.bot2048.watch com.bot2048.live)

die() { echo "error: $*" >&2; exit 1; }

render() {
    # render TEMPLATE DEST
    sed -e "s|@REPO@|$repo|g" -e "s|@HOME@|${HOME:?HOME is unset}|g" "$1" > "$2"
    if command -v plutil >/dev/null 2>&1; then
        plutil -lint "$2" >/dev/null || die "rendered plist is invalid: $2"
    fi
}

mode="install"
out_dir=""
case "${1:-}" in
    "") ;;
    --uninstall) mode="uninstall" ;;
    --render)
        mode="render"
        out_dir="${2:?--render needs a directory}"
        ;;
    *) die "usage: $0 [--uninstall | --render DIR]" ;;
esac

if [[ "$mode" == "render" ]]; then
    mkdir -p "$out_dir"
    for label in "${labels[@]}"; do
        render "$here/$label.plist.template" "$out_dir/$label.plist"
        echo "rendered $out_dir/$label.plist"
    done
    exit 0
fi

[[ "$(uname -s)" == "Darwin" ]] || die "launchd is macOS-only; use --render DIR elsewhere"
uid="$(id -u)"
agents="$HOME/Library/LaunchAgents"
mkdir -p "$agents" "$repo/project/logs"

if [[ "$mode" == "uninstall" ]]; then
    for label in "${labels[@]}"; do
        launchctl bootout "gui/$uid/$label" 2>/dev/null || true
        rm -f "$agents/$label.plist"
        echo "removed $label"
    done
    exit 0
fi

for label in "${labels[@]}"; do
    dest="$agents/$label.plist"
    render "$here/$label.plist.template" "$dest"
    # Re-bootstrap so an edited template takes effect (bootout is a no-op if absent).
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$uid" "$dest"
    launchctl enable "gui/$uid/$label"
    echo "loaded $label  <- $dest"
done

cat <<EOF

LaunchAgents installed for uid $uid (repo: $repo).
  watcher:  running now, respawns on exit (KeepAlive)
  runner:   loaded, NOT started. Start / stop / status:
      launchctl kickstart -k gui/$uid/com.bot2048.live
      launchctl kill TERM  gui/$uid/com.bot2048.live
      launchctl print      gui/$uid/com.bot2048.live | head -40
  logs:     project/logs/launchd-*.log  (+ project/logs/*.jsonl from the app)
Edit the runner's CLI args in the template, then re-run this script.
EOF

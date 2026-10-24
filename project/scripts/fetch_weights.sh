#!/usr/bin/env bash
# Fetch the n-tuple champion weights, which are NOT in git (56 MB).
#
# The bot runs fine without them: the default engine (tools/engine2048) needs no
# weight file at all, and the ntuple Go tests skip themselves when the net is
# absent. The weights are required only for the record engine and for live play
# on the n-tuple net.
#
# Resolution order for the download URL:
#   1. $BOT2048_WEIGHTS_URL, if set (any direct URL)
#   2. the latest GitHub Release asset of this repo's `origin` remote
#
# Usage:
#   scripts/fetch_weights.sh                 # from project/
#   BOT2048_WEIGHTS_URL=https://... scripts/fetch_weights.sh
set -euo pipefail

ASSET="champion-wall-snap-0316.bin"
EXPECTED_MD5="b030c9664436043fd4ae769e7aebb9e2"
EXPECTED_SIZE=56690016

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest_dir="$script_dir/../tools/ntuple/run"
dest="$dest_dir/$ASSET"

check() {
    # Valid iff the size matches and, when a hasher is available, the md5 too.
    [ -f "$1" ] || return 1
    local size
    size=$(stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || echo 0)
    [ "$size" = "$EXPECTED_SIZE" ] || return 1
    if command -v md5sum >/dev/null 2>&1; then
        [ "$(md5sum "$1" | cut -d' ' -f1)" = "$EXPECTED_MD5" ] || return 1
    elif command -v md5 >/dev/null 2>&1; then
        [ "$(md5 -q "$1")" = "$EXPECTED_MD5" ] || return 1
    fi
    return 0
}

if check "$dest"; then
    echo "weights already present and verified: $dest"
    exit 0
fi

if [ -n "${BOT2048_WEIGHTS_URL:-}" ]; then
    url="$BOT2048_WEIGHTS_URL"
else
    origin=$(git -C "$script_dir" remote get-url origin 2>/dev/null || true)
    if [ -z "$origin" ]; then
        echo "error: no git 'origin' remote and \$BOT2048_WEIGHTS_URL is unset." >&2
        echo "Pass the URL explicitly:" >&2
        echo "  BOT2048_WEIGHTS_URL=https://.../$ASSET $0" >&2
        exit 1
    fi
    # git@github.com:owner/repo.git | https://github.com/owner/repo(.git) -> owner/repo
    slug=$(printf '%s\n' "$origin" \
        | sed -e 's#^git@github\.com:##' -e 's#^https://github\.com/##' -e 's#\.git$##')
    url="https://github.com/$slug/releases/latest/download/$ASSET"
    echo "resolved from origin ($slug)"
fi

mkdir -p "$dest_dir"
echo "downloading $ASSET (56 MB) ..."
tmp="$dest.part"
if command -v curl >/dev/null 2>&1; then
    curl -fL --progress-bar -o "$tmp" "$url"
elif command -v wget >/dev/null 2>&1; then
    wget -q --show-progress -O "$tmp" "$url"
else
    echo "error: need curl or wget" >&2
    exit 1
fi

if ! check "$tmp"; then
    rm -f "$tmp"
    echo "error: downloaded file failed verification (expected md5 $EXPECTED_MD5," >&2
    echo "       size $EXPECTED_SIZE). Wrong asset, or the release does not exist yet:" >&2
    echo "       $url" >&2
    exit 1
fi

mv -f "$tmp" "$dest"
echo "ok: $dest"
echo
echo "The engine picks this file up automatically on an arg-less launch."
echo "To use it explicitly:  ntuple -weights run/$ASSET"
echo "Or set:                BOT2048_NTUPLE_WEIGHTS=$dest"

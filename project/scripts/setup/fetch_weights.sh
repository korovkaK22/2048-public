#!/usr/bin/env bash
# Fetch the n-tuple net weights, which are NOT in git (56 MB each), into
# tools/ntuple/run/. Both are needed on a live host:
#
#   w-l0.003.bin                 the CHAMPION = canonical LIVE net
#                                (config/strategies/champion-live.json: what the
#                                i7 production actually played, + record engine,
#                                depth 4). No pinned checksum: verified as "a
#                                real binary of plausible size".
#   champion-wall-snap-0316.bin  the WALL net = record-parity alternative
#                                (wall-record.json); md5 + size pinned.
#
# The bot runs without them for sims: the default engine (tools/engine2048)
# needs no weight file, and the ntuple Go tests skip themselves when a net is
# absent. scripts/live/live.sh REQUIRES w-l0.003.bin (or whatever
# BOT2048_NTUPLE_WEIGHTS points at) and pins it explicitly, because an
# arg-less engine launch picks the net by probing run/ for filenames
# (serve.go weightCandidates) -- provenance must not depend on dir contents.
#
# URL resolution (per asset):
#   1. $BOT2048_WEIGHTS_BASE_URL/<asset>, if set (a directory-like URL)
#   2. $BOT2048_WEIGHTS_URL, if set -- a direct URL for the WALL net only
#      (kept for backwards compatibility with the single-asset script)
#   3. the latest GitHub Release asset of this repo's `origin` remote
#
# Exit code: 0 when every asset is present and verified; 1 otherwise. A
# missing asset is never silent: each one gets its own explicit message, and
# w-l0.003.bin in particular says how to get it if the release lacks it
# (copy project/tools/ntuple/run/w-l0.003.bin from the Windows box).
#
# Usage (from project/):
#   scripts/setup/fetch_weights.sh
#   BOT2048_WEIGHTS_BASE_URL=https://host/dir scripts/setup/fetch_weights.sh
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest_dir="$script_dir/../../tools/ntuple/run"   # scripts/setup/ -> project/tools/ntuple/run

# name | md5 (or "-" = unpinned) | size (or "-" = unpinned, >= MIN_PLAUSIBLE) | required|optional
# Both are "required" on a live host; the field stays so a future asset can be
# optional (an optional miss is a WARNING and exit 0, a required miss exit 1).
ASSETS=(
    "w-l0.003.bin|-|-|required"
    "champion-wall-snap-0316.bin|b030c9664436043fd4ae769e7aebb9e2|56690016|required"
)
MIN_PLAUSIBLE_SIZE=$((40 * 1024 * 1024))   # every net of this family is ~56 MB

file_size() {
    stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || echo 0
}

file_md5() {
    if command -v md5sum >/dev/null 2>&1; then
        md5sum "$1" | cut -d' ' -f1
    elif command -v md5 >/dev/null 2>&1; then
        md5 -q "$1"
    else
        echo ""   # no hasher: size check only
    fi
}

# check FILE MD5 SIZE -> 0 iff the file passes the pinned (or plausibility) check.
check() {
    local file="$1" md5="$2" size="$3" actual
    [ -f "$file" ] || return 1
    actual=$(file_size "$file")
    if [ "$size" != "-" ]; then
        [ "$actual" = "$size" ] || return 1
    else
        [ "$actual" -ge "$MIN_PLAUSIBLE_SIZE" ] || return 1
    fi
    if [ "$md5" != "-" ]; then
        local got
        got=$(file_md5 "$file")
        [ -z "$got" ] || [ "$got" = "$md5" ] || return 1
    fi
    return 0
}

resolve_url() {
    # resolve_url ASSET REQUIRED -> prints the URL or returns 1
    local asset="$1" required="$2"
    if [ -n "${BOT2048_WEIGHTS_BASE_URL:-}" ]; then
        printf '%s/%s\n' "${BOT2048_WEIGHTS_BASE_URL%/}" "$asset"
        return 0
    fi
    if [ "$asset" = "champion-wall-snap-0316.bin" ] && [ -n "${BOT2048_WEIGHTS_URL:-}" ]; then
        printf '%s\n' "$BOT2048_WEIGHTS_URL"
        return 0
    fi
    local origin slug
    origin=$(git -C "$script_dir" remote get-url origin 2>/dev/null || true)
    if [ -z "$origin" ]; then
        echo "error: no git 'origin' remote and no \$BOT2048_WEIGHTS_BASE_URL." >&2
        echo "Pass the location explicitly:" >&2
        echo "  BOT2048_WEIGHTS_BASE_URL=https://.../dir $0" >&2
        return 1
    fi
    # git@github.com:owner/repo.git | https://github.com/owner/repo(.git) -> owner/repo
    slug=$(printf '%s\n' "$origin" \
        | sed -e 's#^git@github\.com:##' -e 's#^https://github\.com/##' -e 's#\.git$##')
    printf 'https://github.com/%s/releases/latest/download/%s\n' "$slug" "$asset"
}

download() {
    # download URL DEST -> curl/wget exit status
    if command -v curl >/dev/null 2>&1; then
        curl -fL --progress-bar -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --show-progress -O "$2" "$1"
    else
        echo "error: need curl or wget" >&2
        return 1
    fi
}

mkdir -p "$dest_dir"
failed_required=0
missing_optional=()

for spec in "${ASSETS[@]}"; do
    IFS='|' read -r asset md5 size required <<<"$spec"
    dest="$dest_dir/$asset"

    if check "$dest" "$md5" "$size"; then
        echo "ok ($required): already present and verified: $dest"
        continue
    fi

    if ! url=$(resolve_url "$asset" "$required"); then
        if [ "$required" = "required" ]; then failed_required=1; else missing_optional+=("$asset"); fi
        continue
    fi

    echo "downloading $asset ($required) <- $url"
    tmp="$dest.part"
    if ! download "$url" "$tmp"; then
        rm -f "$tmp"
        echo "error: could not download $asset ($required) from" >&2
        echo "       $url" >&2
        echo "       (private repo without a token? asset not in the release? -> set" >&2
        echo "        BOT2048_WEIGHTS_BASE_URL to a dir that has it)" >&2
        if [ "$asset" = "w-l0.003.bin" ]; then
            echo "       w-l0.003.bin is the LIVE net (champion-live.json). If the release does" >&2
            echo "       not publish it, copy it from the Windows box:" >&2
            echo "         scp <win>:monobank-2048/project/tools/ntuple/run/w-l0.003.bin $dest_dir/" >&2
        fi
        if [ "$required" = "required" ]; then failed_required=1; else missing_optional+=("$asset"); fi
        continue
    fi

    if ! check "$tmp" "$md5" "$size"; then
        rm -f "$tmp"
        echo "error: $asset failed verification" >&2
        if [ "$md5" != "-" ]; then
            echo "       expected md5 $md5, size $size. Wrong asset or a broken release:" >&2
        else
            echo "       expected a binary >= $MIN_PLAUSIBLE_SIZE bytes (got an HTML error page?):" >&2
        fi
        echo "       $url" >&2
        if [ "$required" = "required" ]; then failed_required=1; else missing_optional+=("$asset"); fi
        continue
    fi

    mv -f "$tmp" "$dest"
    echo "ok ($required): $dest"
done

echo
if [ "$failed_required" -ne 0 ]; then
    echo "FAILED: a required net is missing (see errors above)." >&2
    echo "        scripts/live/live.sh refuses to start without run/w-l0.003.bin." >&2
    exit 1
fi
if [ "${#missing_optional[@]}" -gt 0 ]; then
    echo "WARNING: optional asset(s) not fetched: ${missing_optional[*]} (see notes above)."
fi
echo "Live (champion-live):  BOT2048_NTUPLE_WEIGHTS=$dest_dir/w-l0.003.bin  (default in scripts/live/live.sh)"
echo "Wall net (wall-record): BOT2048_NTUPLE_WEIGHTS=$dest_dir/champion-wall-snap-0316.bin scripts/live/live.sh ..."

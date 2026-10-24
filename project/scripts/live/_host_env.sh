#!/usr/bin/env bash
# Shared host bootstrap for the live launchers (scripts/live/live.sh, scripts/live/watch.sh).
# SOURCED, not executed:  source "$(dirname "$0")/_host_env.sh"
#
# On source it:
#   1. cd's into project/ (logs/, data/, control/, tools/ are cwd-relative);
#   2. on macOS prepends Homebrew to PATH -- launchd jobs start with a bare
#      /usr/bin:/bin PATH, so adb/ffmpeg/scrcpy/go would not be found (audit A3);
#   3. picks the venv interpreter into $PY (POSIX .venv/bin/python or Windows
#      .venv/Scripts/python.exe) and pins PYTHONPATH=src so a worktree runs ITS
#      checkout, not the editable install of the main copy;
#   4. defines helpers: die, source_secrets, is_macos, host_arch.
#
# It never prints secrets and never touches the network.

if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "error: _host_env.sh is meant to be sourced, not executed" >&2
    exit 2
fi

die() {
    echo "error: $*" >&2
    exit 1
}

is_macos() { [[ "$(uname -s)" == "Darwin" ]]; }

# arm64 (Apple Silicon, aarch64 Linux) vs x86_64 -- used for per-arch engine knobs.
host_arch() {
    case "$(uname -m)" in
        arm64 | aarch64) echo arm64 ;;
        x86_64 | amd64) echo amd64 ;;
        *) uname -m ;;
    esac
}

# Export every KEY=VALUE of an env file (if it exists) into the environment.
# `set -a` marks sourced assignments for export; works with or without `export`.
source_secrets() {
    local file="$1"
    if [[ -f "$file" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$file"
        set +a
    fi
}

_host_scripts_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$_host_scripts_dir/../.." || die "cannot cd to project/"   # scripts/live/ -> project/
unset _host_scripts_dir

# Absolute project/ path in a form the *native* interpreter understands: on
# Git Bash $PWD is an MSYS path (/c/...), which a Windows python does not read
# back from an env var -- cygpath -m gives C:/... that both sides accept.
PROJECT_DIR="$PWD"
if command -v cygpath >/dev/null 2>&1; then
    PROJECT_DIR="$(cygpath -m "$PWD")"
fi
export PROJECT_DIR

if is_macos; then
    # Last prepended wins the front: /opt/homebrew/bin ends up first.
    for _d in /usr/local/bin /opt/homebrew/sbin /opt/homebrew/bin; do
        [[ -d "$_d" ]] && PATH="$_d:$PATH"
    done
    unset _d
    export PATH
fi

if [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
elif [[ -x ".venv/Scripts/python.exe" ]]; then
    PY=".venv/Scripts/python.exe"
else
    die "no venv at $PWD/.venv -- run scripts/macos/setup.sh (macOS) or create it (docs/macos-setup.md)"
fi
export PY

export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUTF8=1   # Ukrainian log lines / rich output regardless of locale

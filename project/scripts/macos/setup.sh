#!/usr/bin/env bash
# One-shot (idempotent) host setup for the Mac mini live host.
#
#   scripts/macos/setup.sh [--no-brew] [--no-weights] [--uv]
#
# Steps (each skipped when already satisfied):
#   1. Homebrew packages: android-platform-tools (cask), scrcpy, ffmpeg, go,
#      python@3.12. Brew's Python ships proper CA certs -- python.org's build
#      gives CERTIFICATE_VERIFY_FAILED in tgnotify (audit A8).
#   2. project/.venv on python3.12 (or `uv venv` with --uv) + pip install -e ".[dev,device]"
#   3. both Go engines built NATIVELY for arm64 with `go build -pgo=auto`
#      (default.pgo is gitignored and x86-generated -- regenerate per BUILD.md;
#      without it -pgo=auto is a no-op, which is fine for a first run)
#   4. scripts/fetch_weights.sh (champion-wall-snap-0316.bin, w-l0.003.bin)
#   5. prints the pmset / caffeinate / launchd commands -- it never runs sudo.
#
# After this: docs/macos-setup.md "first run" checklist (adb key, adb devices,
# `scripts/live.sh once`, `scripts/macos/install.sh`).
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "error: this script is for macOS" >&2; exit 1; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project="$(cd "$here/../.." && pwd)"
cd "$project"

do_brew=1
do_weights=1
use_uv=0
for arg in "$@"; do
    case "$arg" in
        --no-brew) do_brew=0 ;;
        --no-weights) do_weights=0 ;;
        --uv) use_uv=1 ;;
        *) echo "usage: $0 [--no-brew] [--no-weights] [--uv]" >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }

# --- 1. Homebrew ---------------------------------------------------------------
if [[ $do_brew -eq 1 ]]; then
    step "Homebrew packages"
    if ! command -v brew >/dev/null 2>&1; then
        if [[ -x /opt/homebrew/bin/brew ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        else
            echo "Homebrew is not installed. Install it first:" >&2
            echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' >&2
            exit 1
        fi
    fi
    # One adb only (audit A6): the cask, never tools/platform-tools alongside it.
    brew list --cask android-platform-tools >/dev/null 2>&1 || brew install --cask android-platform-tools
    for pkg in scrcpy ffmpeg go python@3.12; do
        brew list --formula "$pkg" >/dev/null 2>&1 || brew install "$pkg"
    done
    if [[ $use_uv -eq 1 ]]; then
        brew list --formula uv >/dev/null 2>&1 || brew install uv
    fi
    # Make sure THIS shell sees brew's bin even if the user's profile does not yet.
    eval "$(brew shellenv)"
fi

# --- 2. venv -------------------------------------------------------------------
step "Python venv (project/.venv)"
if [[ ! -x .venv/bin/python ]]; then
    if [[ $use_uv -eq 1 ]]; then
        uv venv --python 3.12 .venv
    else
        py312="$(command -v python3.12 || true)"
        [[ -n "$py312" ]] || py312="$(brew --prefix python@3.12)/bin/python3.12"
        [[ -x "$py312" ]] || { echo "error: python3.12 not found (brew install python@3.12)" >&2; exit 1; }
        "$py312" -m venv .venv
    fi
    echo "created .venv with $(.venv/bin/python --version)"
else
    echo "exists: .venv ($(.venv/bin/python --version))"
fi
if [[ $use_uv -eq 1 ]]; then
    uv pip install --python .venv/bin/python -e ".[dev,device]"
else
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet -e ".[dev,device]"
fi
echo "installed: bot2048[dev,device]"

# --- 3. Go engines (native arm64) ----------------------------------------------
step "Go engines (native $(uname -m), -pgo=auto)"
command -v go >/dev/null 2>&1 || { echo "error: go not on PATH (brew install go)" >&2; exit 1; }
(cd tools/engine2048 && go build -pgo=auto -o engine2048 . && echo "built tools/engine2048/engine2048")
(cd tools/ntuple && go build -pgo=auto -o ntuple . && echo "built tools/ntuple/ntuple")
[[ -f tools/ntuple/default.pgo ]] || echo "note: tools/ntuple/default.pgo absent -> no PGO yet (regenerate on this arch: tools/ntuple/BUILD.md)"

# --- 4. weights ----------------------------------------------------------------
if [[ $do_weights -eq 1 ]]; then
    step "n-tuple weights"
    scripts/fetch_weights.sh || echo "warning: fetch_weights.sh reported a problem -- see above (live.sh refuses to start without the wall net)"
fi

# --- 5. power / launchd guidance (printed, never executed) ---------------------
step "Done. Manual steps (need sudo or the GUI):"
cat <<'EOF'
  # never sleep, no Power Nap, come back after a power cut (audit A7):
  sudo pmset -a sleep 0 disksleep 0 powernap 0 autorestart 1 displaysleep 10
  pmset -g custom                      # verify
  # System Settings -> General -> Software Update -> Automatic updates: OFF
  # System Settings -> Lock Screen -> require password: adjust so a reboot can reach the LaunchAgent
  # (FileVault keeps the agent waiting for the first login -- owner decision, audit A10)

  # adb host key from the Windows box (phone already trusts it):
  #   copy %USERPROFILE%\.android\adbkey and adbkey.pub -> ~/.android/  (chmod 600 adbkey)
  adb devices                          # must show the phone as "device"

  # first dry run, then the LaunchAgents:
  scripts/live.sh once --capture apk
  scripts/macos/install.sh
EOF

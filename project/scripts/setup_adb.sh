#!/usr/bin/env bash
# Idempotent Android tooling bootstrap for the live bot -- Windows (Git Bash),
# macOS and Linux.
#
#   macOS with Homebrew  : `brew install --cask android-platform-tools` +
#                          `brew install scrcpy`. ONE adb on the box (audit A6):
#                          nothing is downloaded into tools/, the CLI resolves
#                          both from PATH (scripts/live.sh puts brew on PATH).
#   everything else      : downloads into project/tools/ (gitignored):
#                          * Android platform-tools (adb) -- official Google zip
#                            for this OS
#                          * scrcpy -- latest GitHub release asset for this
#                            OS/arch (win64 | macos-aarch64/x86_64 | linux-x86_64)
# then verifies `adb version` and prints USB-debugging instructions.
#
# Usage:  scripts/setup_adb.sh [--force]
#   --force  re-download even if the tools are already present (no-op for brew)

set -euo pipefail

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
    FORCE=1
elif [[ -n "${1:-}" ]]; then
    echo "usage: $0 [--force]" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TOOLS_DIR="$PROJECT_ROOT/tools"

# --- per-OS selection ------------------------------------------------------------
os="$(uname -s)"
arch="$(uname -m)"
case "$os" in
    MINGW* | MSYS* | CYGWIN*)
        PT_OS="windows"; EXE=".exe"; SCRCPY_ASSET='scrcpy-win64-[^"]*\.zip' ;;
    Darwin)
        PT_OS="darwin"; EXE=""
        case "$arch" in
            arm64) SCRCPY_ASSET='scrcpy-macos-aarch64-[^"]*\.tar\.gz' ;;
            *) SCRCPY_ASSET='scrcpy-macos-x86_64-[^"]*\.tar\.gz' ;;
        esac ;;
    Linux)
        PT_OS="linux"; EXE=""; SCRCPY_ASSET='scrcpy-linux-x86_64-[^"]*\.tar\.gz' ;;
    *)
        echo "ERROR: unsupported host OS: $os" >&2; exit 1 ;;
esac

# --- macOS + Homebrew: install from brew, never into tools/ ----------------------
if [[ "$os" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    if brew list --cask android-platform-tools >/dev/null 2>&1; then
        echo "[skip] android-platform-tools (brew cask) already installed"
    else
        echo "[brew] install --cask android-platform-tools"
        brew install --cask android-platform-tools
    fi
    if brew list --formula scrcpy >/dev/null 2>&1; then
        echo "[skip] scrcpy (brew) already installed"
    else
        echo "[brew] install scrcpy"
        brew install scrcpy
    fi
    if [[ -d "$TOOLS_DIR/platform-tools" ]]; then
        echo "WARNING: $TOOLS_DIR/platform-tools exists -- the CLI prefers it over PATH adb." >&2
        echo "         Two adb builds fight over the server; remove it:  rm -rf $TOOLS_DIR/platform-tools" >&2
    fi
    ADB="$(command -v adb)"
    SCRCPY_BIN="$(command -v scrcpy)"
    echo "[chk]  adb version:"
    "$ADB" version
else
    # --- download path (Windows / Linux / macOS without brew) --------------------
    mkdir -p "$TOOLS_DIR"
    PLATFORM_TOOLS_URL="https://dl.google.com/android/repository/platform-tools-latest-$PT_OS.zip"
    SCRCPY_API_URL="https://api.github.com/repos/Genymobile/scrcpy/releases/latest"

    ADB="$TOOLS_DIR/platform-tools/adb$EXE"
    if [[ -f "$ADB" && $FORCE -eq 0 ]]; then
        echo "[skip] platform-tools already installed: $ADB"
    else
        echo "[dl]   platform-tools <- $PLATFORM_TOOLS_URL"
        curl -fSL --retry 3 -o "$TOOLS_DIR/platform-tools.zip" "$PLATFORM_TOOLS_URL"
        rm -rf "$TOOLS_DIR/platform-tools"
        unzip -oq "$TOOLS_DIR/platform-tools.zip" -d "$TOOLS_DIR"
        rm -f "$TOOLS_DIR/platform-tools.zip"
        echo "[ok]   platform-tools -> $TOOLS_DIR/platform-tools"
    fi

    echo "[chk]  adb version:"
    "$ADB" version

    SCRCPY_DIR="$TOOLS_DIR/scrcpy"
    SCRCPY_BIN="$SCRCPY_DIR/scrcpy$EXE"
    if [[ -f "$SCRCPY_BIN" && $FORCE -eq 0 ]]; then
        echo "[skip] scrcpy already installed: $SCRCPY_BIN"
    else
        echo "[dl]   resolving latest scrcpy release ($SCRCPY_ASSET)..."
        SCRCPY_URL="$(curl -fsSL --retry 3 "$SCRCPY_API_URL" \
            | grep -oE "\"browser_download_url\": *\"[^\"]*$SCRCPY_ASSET\"" \
            | grep -oE 'https[^"]*' | head -n1)"
        if [[ -z "$SCRCPY_URL" ]]; then
            echo "ERROR: could not resolve a scrcpy asset matching $SCRCPY_ASSET from the GitHub API" >&2
            echo "       download manually: https://github.com/Genymobile/scrcpy/releases" >&2
            exit 1
        fi
        echo "[dl]   scrcpy <- $SCRCPY_URL"
        ARCHIVE="$TOOLS_DIR/scrcpy-release.${SCRCPY_URL##*.}"
        curl -fSL --retry 3 -o "$ARCHIVE" "$SCRCPY_URL"
        EXTRACT_DIR="$TOOLS_DIR/.scrcpy-extract"
        rm -rf "$EXTRACT_DIR"
        mkdir -p "$EXTRACT_DIR"
        case "$SCRCPY_URL" in
            *.zip) unzip -oq "$ARCHIVE" -d "$EXTRACT_DIR" ;;
            *) tar -xzf "$ARCHIVE" -C "$EXTRACT_DIR" ;;
        esac
        rm -f "$ARCHIVE"
        # the archive contains a single versioned folder (scrcpy-<os>-vX.Y) -> tools/scrcpy
        INNER="$(find "$EXTRACT_DIR" -mindepth 1 -maxdepth 1 -type d | head -n1)"
        rm -rf "$SCRCPY_DIR"
        mv "${INNER:-$EXTRACT_DIR}" "$SCRCPY_DIR"
        rm -rf "$EXTRACT_DIR"
        echo "[ok]   scrcpy -> $SCRCPY_DIR"
    fi
fi

cat <<EOF

============================================================
Tooling ready:
  adb:    $ADB
  scrcpy: $SCRCPY_BIN

Next: enable USB debugging on the phone (one time):
  1. Settings -> About phone -> tap "Build number" 7 times
     (unlocks Developer options)
  2. Settings -> Developer options -> enable "USB debugging"
     (for long sessions also enable "Stay awake while charging")
  3. Connect the phone via USB; on the phone confirm the
     "Allow USB debugging?" prompt (check "Always allow")
     -- or copy the already-trusted host key (~/.android/adbkey*)
     from the old machine and skip the prompt (docs/macos-setup.md)
  4. Verify:
       "$ADB" devices
     The device must be listed with state "device"
     (not "unauthorized"/"offline").
  5. Optional screen mirroring while the bot plays:
       "$SCRCPY_BIN"

Full walkthrough: ../docs/live-setup.md  (macOS: ../docs/macos-setup.md)
============================================================
EOF

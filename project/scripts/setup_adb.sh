#!/usr/bin/env bash
# Idempotent Android tooling bootstrap for the live bot (Git Bash / Windows).
#
# Downloads into project/tools/ (gitignored):
#   * Android platform-tools (adb)  -- official Google zip
#   * scrcpy (screen mirroring)     -- latest GitHub release (win64 zip)
# then verifies `adb version` and prints USB-debugging instructions.
#
# Usage:  scripts/setup_adb.sh [--force]
#   --force  re-download even if the tools are already present

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
mkdir -p "$TOOLS_DIR"

PLATFORM_TOOLS_URL="https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
SCRCPY_API_URL="https://api.github.com/repos/Genymobile/scrcpy/releases/latest"

# --- platform-tools (adb) ---------------------------------------------------
ADB="$TOOLS_DIR/platform-tools/adb.exe"
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

# --- scrcpy ------------------------------------------------------------------
SCRCPY_DIR="$TOOLS_DIR/scrcpy"
if [[ -f "$SCRCPY_DIR/scrcpy.exe" && $FORCE -eq 0 ]]; then
    echo "[skip] scrcpy already installed: $SCRCPY_DIR/scrcpy.exe"
else
    echo "[dl]   resolving latest scrcpy release..."
    SCRCPY_URL="$(curl -fsSL --retry 3 "$SCRCPY_API_URL" \
        | grep -oE '"browser_download_url": *"[^"]*scrcpy-win64-[^"]*\.zip"' \
        | grep -oE 'https[^"]*' | head -n1)"
    if [[ -z "$SCRCPY_URL" ]]; then
        echo "ERROR: could not resolve a scrcpy-win64 zip from the GitHub API" >&2
        echo "       download manually: https://github.com/Genymobile/scrcpy/releases" >&2
        exit 1
    fi
    echo "[dl]   scrcpy <- $SCRCPY_URL"
    curl -fSL --retry 3 -o "$TOOLS_DIR/scrcpy.zip" "$SCRCPY_URL"
    EXTRACT_DIR="$TOOLS_DIR/.scrcpy-extract"
    rm -rf "$EXTRACT_DIR"
    mkdir -p "$EXTRACT_DIR"
    unzip -oq "$TOOLS_DIR/scrcpy.zip" -d "$EXTRACT_DIR"
    rm -f "$TOOLS_DIR/scrcpy.zip"
    # the zip contains a single versioned folder (scrcpy-win64-vX.Y) -> tools/scrcpy
    INNER="$(find "$EXTRACT_DIR" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    rm -rf "$SCRCPY_DIR"
    mv "${INNER:-$EXTRACT_DIR}" "$SCRCPY_DIR"
    rm -rf "$EXTRACT_DIR"
    echo "[ok]   scrcpy -> $SCRCPY_DIR"
fi

cat <<EOF

============================================================
Tooling ready:
  adb:    $ADB
  scrcpy: $SCRCPY_DIR/scrcpy.exe

Next: enable USB debugging on the phone (one time):
  1. Settings -> About phone -> tap "Build number" 7 times
     (unlocks Developer options)
  2. Settings -> Developer options -> enable "USB debugging"
     (for long sessions also enable "Stay awake while charging")
  3. Connect the phone via USB; on the phone confirm the
     "Allow USB debugging?" prompt (check "Always allow")
  4. Verify:
       tools/platform-tools/adb.exe devices
     The device must be listed with state "device"
     (not "unauthorized"/"offline").
  5. Optional screen mirroring while the bot plays:
       tools/scrcpy/scrcpy.exe

Full walkthrough: ../docs/live-setup.md
============================================================
EOF

#!/usr/bin/env bash
# Live launcher: `bot2048 live <play|once|research>` with the production engine
# environment, from any host (macOS primary, Linux/Windows Git Bash still work).
#
# Usage (from anywhere; the script cd's into project/ itself):
#   scripts/live.sh --strategy go --depth 4 --budget-ms 250 --capture apk --auto-login --games 0
#   scripts/live.sh once --capture apk            # dry run: one capture -> state -> decide
#   scripts/live.sh research --games 20           # spawn-rule measurement
# The first word selects the `live` sub-command (default: play); everything
# else is passed through to the CLI untouched.
#
# What it sets up (and why):
#   * cwd = project/, Homebrew on PATH, venv python, PYTHONPATH=src  (_host_env.sh)
#   * MONO_PIN from ../.secrets/device.env  (opt-in --auto-login reads it)
#   * the n-tuple engine binary + the canonical live net + the record-engine
#     levers as env vars for the Go engine (it reads them once at startup and
#     reports them via `version`, so a run's provenance is in the DB)
#   * BOT2048_EVAL_MLP=0 on arm64 (audit B3: the MLP gather-split leaf is an
#     x86 win and regressed 8-15% on Apple M4 -- experiment-log 2026-07-12)
#   * BOT2048_SEARCH_WORKERS = number of PERFORMANCE cores on macOS (audit B1:
#     the serve default NumCPU-2 = 8 on a 4P+6E M4 drags E-cores into the budget
#     search tail); elsewhere the engine's own default is kept
#   * caffeinate -is on macOS: no idle/system sleep while the runner is alive
#
# TODO(StrategySpec): every BOT2048_* knob below is a stop-gap. Once the
# StrategySpec factory lands (config/strategies/champion-live.json, being built
# by another agent), the engine binary, the weights path and the record levers
# move into that JSON and this script shrinks to "cd + venv + secrets + exec".
# Keep this block in sync with that file until then; do NOT add new knobs here.
set -euo pipefail

# shellcheck source=_host_env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_host_env.sh"

source_secrets "../.secrets/device.env"          # MONO_PIN (never printed)

# --- engine binary + weights (canonical live net) ----------------------------
exe=""
case "$(uname -s)" in MINGW* | MSYS* | CYGWIN*) exe=".exe" ;; esac

engine_bin="${BOT2048_ENGINE2048_BIN:-$PROJECT_DIR/tools/ntuple/ntuple$exe}"
[[ -x "$engine_bin" ]] || die "n-tuple engine not built: $engine_bin
  build it:  (cd tools/ntuple && go build -pgo=auto -o ntuple$exe .)   # or scripts/macos/setup.sh
  NOTE: do not let go_engine.py auto-build it -- that path builds tools/engine2048, not ntuple."
export BOT2048_ENGINE2048_BIN="$engine_bin"

weights="${BOT2048_NTUPLE_WEIGHTS:-$PROJECT_DIR/tools/ntuple/run/champion-wall-snap-0316.bin}"
[[ -f "$weights" ]] || die "weights not found: $weights
  fetch them:  scripts/fetch_weights.sh"
# Explicit on purpose: an arg-less engine launch probes w-l0.003.bin FIRST
# (serve.go weightCandidates), which is the OLD champion, not the record net.
export BOT2048_NTUPLE_WEIGHTS="$weights"

# --- record engine (docs/record-engine.md "validated winning config") --------
# docs/knowledge/record-is-midgame-mass.md:21-22 -- depth 4, EMPTIES=8,
# NODES=6e6, PCONF=0.6, EARLYFIRE=300, EARLY_EMPTIES=1. `${X:-default}` so a
# one-off override from the shell still wins.
export BOT2048_RECORD_ENGINE="${BOT2048_RECORD_ENGINE:-1}"
export BOT2048_TARGET="${BOT2048_TARGET:-49601}"
export BOT2048_EXACT_EMPTIES="${BOT2048_EXACT_EMPTIES:-8}"
export BOT2048_EXACT_NODES="${BOT2048_EXACT_NODES:-6000000}"
export BOT2048_TARGET_PCONF="${BOT2048_TARGET_PCONF:-0.6}"
export BOT2048_EXACT_EARLYFIRE="${BOT2048_EXACT_EARLYFIRE:-300}"
export BOT2048_EXACT_EARLY_EMPTIES="${BOT2048_EXACT_EARLY_EMPTIES:-1}"

# --- per-arch / per-host engine tuning ---------------------------------------
if [[ "$(host_arch)" == "arm64" ]]; then
    export BOT2048_EVAL_MLP="${BOT2048_EVAL_MLP:-0}"
fi

if is_macos && [[ -z "${BOT2048_SEARCH_WORKERS:-}" ]]; then
    # Performance-core count (4 on Mac mini M4). Falls back to the engine default
    # when sysctl has no such key (Intel Mac).
    if perf_cores="$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null)" && [[ "$perf_cores" =~ ^[0-9]+$ ]]; then
        export BOT2048_SEARCH_WORKERS="$perf_cores"
    fi
fi

# --- sub-command + args -------------------------------------------------------
sub="play"
case "${1:-}" in
    play | once | research)
        sub="$1"
        shift
        ;;
esac

# --- keep the Mac awake for exactly the lifetime of this process -------------
# `exec` below keeps our pid, so `caffeinate -w $$` releases the assertion the
# moment the runner exits (clean, FATAL or killed) -- no stray caffeinate.
# The runner stays launchd's direct child, so SIGTERM reaches it directly.
if is_macos && command -v caffeinate >/dev/null 2>&1; then
    caffeinate -is -w $$ &
fi

exec "$PY" -m bot2048.cli live "$sub" "$@"

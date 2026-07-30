---
name: mac-max-power-commands
description: "macOS commands: keep MacBook awake for long sim runs (lid-aware sleep + max power), and how to revert"
metadata:
  node_type: memory
  type: reference
  originSessionId: 9994db1d-76a1-45df-a78d-0d55139499dc
---

Andrii runs heavy local simulations on an Apple Silicon MacBook (14 cores, 24GB). Asked repeatedly — will ask for these again.

## LID-AWARE no-sleep (his preferred, 2026-07-21): lid OPEN → never sleep (screen may turn off); lid CLOSED → sleep
```bash
sudo pmset -c sleep 0 displaysleep 10 && killall caffeinate
```
- `sleep 0` = on AC, no idle system-sleep while lid is open. `displaysleep 10` = screen off after 10 min (compute keeps running). `killall caffeinate` = REMOVE any `caffeinate` assertion (caffeinate -dimsu blocks even lid-close sleep, which he does NOT want). Clamshell/lid-close sleep stays on because `disablesleep`=0 (default).
- MUST stay on AC power (battery throttles + can sleep) and lid OPEN for an overnight run (closing the lid sleeps it and pauses compute).

## REVERT to normal
```bash
sudo pmset -c sleep 1 displaysleep 10
```
(restores his prior captured value sleep=1; or `sudo pmset restoredefaults` for factory.)

## Blunt "never sleep at all, even lid closed" (older approach — only if he wants lid-closed to keep running)
```bash
sudo pmset -a lowpowermode 0 && caffeinate -dimsu &   # turn OFF: killall caffeinate
```
Use the lid-aware version above by default; this blunt one keeps running with the lid shut (external-display/headless style), which he explicitly did NOT want on 2026-07-21.

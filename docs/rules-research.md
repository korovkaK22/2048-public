# Game-rules research — monobank 2048 clone

The clone (inside `com.ftband.mono`) looks like classic 2048 but its exact
rules are undocumented. Simulations are only as good as the rules they
assume, so every assumption below is treated as an OPEN QUESTION until
measured. Collected facts live here; raw UI observations in
`device-ui-notes.md`.

## Established facts (screenshots in `screens/` + user observations)

- Package `com.ftband.mono`, reference screen **1080×2400**.
- Board **4×4**; header «Рахунок / Рекорд», timer above the board, cat
  mascot overlapping the board top; «Нова гра» bottom-center, sound button
  bottom-right, close «×» top-left.
- Tile palette is clearly distinct per value: 2=dark gray,
  4=slate gray-violet, 8=blue, 16=teal, 32=green, 64=orange
  (128+ to be captured during calibration). Recognition is pure
  color-lookup, no OCR.
- **Initial spawn (manual observation, 2026-07-10)**: after «Нова гра» the
  board gets **2 tiles in random cells**, normally both value 2, with a small
  chance of a 4 — consistent with classic 2048 start. Sample size is a
  handful of manual restarts, so exact P(4) and cell uniformity still come
  from research mode (Q1–Q3 below).
- Game over shows a popup with a button, **BUT** — known bug — the game may
  NOT stop when no moves remain. The bot therefore always detects game over
  itself via `core.engine.is_game_over` on the parsed board; popup detection
  is only for screen-state handling.
- **2048 stop-rule (RESOLVED, confirmed live 2026-07-11): reaching a 2048 tile
  ENDS the game, and the merge that creates it AWARDS +2048 to «Рахунок»
  BEFORE the game stops.** This is the load-bearing assumption behind farm mode
  (the whole reason 2048 is a taboo: you must accumulate score across many high
  tiles and only cash out with a deliberate final 2048 merge). Verified in three
  ways: (a) three separate classic (`--strategy go`, no `--farm`) live games each
  merged two 1024s → 2048 and the app immediately left GAME state — the on-device
  APK reported `not_in_game=True` on the very next frame and the runner logged
  `live.win_popup_2048` + `live.session_done` (games ended, never continued);
  (b) the user filmed the win with an external camera (the win/leaderboard screen
  is FLAG_SECURE — `adb screencap` returns 0 bytes during the win transition, so
  it cannot be screenshotted from the host) and directly observed the score jump
  by +2048 on the final merge; (c) it is exactly classic-2048 scoring (Q5), which
  awards the merged-tile value on every merge including the 2048 one. **Strategy
  consequence:** the max leaderboard score is (accumulated score across the board)
  + 2048 from the terminal cash-out merge; a game NEVER ends on its own timer
  (timer is cosmetic, Q9) — it ends by board deadlock OR by this deliberate 2048
  merge.
- **Cash-out cascade (CONFIRMED, verified on device by Andrii 2026-07-11): the
  endgame sequence 512+512 → 1024, then 1024+1024 → 2048 is a legal, scoring
  move sequence.** A deliberate 1024+1024 → 2048 merge DOES credit +2048 to
  «Рахунок» AND ends the game, with that terminal score counted on the
  leaderboard (directly observed on the physical device). This closes the last
  open question about the cash-out mechanic: the maximizer can bank the final
  merges (512+512→1024) and then fire the terminal 2048 with the +2048 credited
  — the whole cash-out chain is a real, scored path, not just the single final
  merge. Green-lights the endgame cash-out / doom-oracle timing work
  (see `experiment-log.md`, doom-oracle entry).
- **Score formula (Q5, RESOLVED 2026-07-11): classic cumulative 2048** — the
  header «Рахунок» is the running sum of merged-tile values (+`2^(r+1)` on each
  merge of two `2^r` tiles; a spawned tile adds 0 to the score). It is **NOT**
  the board tile-value sum, and NOT the engine's internal `sSumBoard` S-sum
  proxy. See Q5 below for the proof. **Reporting implication:** the sim's
  `summary.final_score` (`sim/runner.py`: `score += result.score_gained`, where
  `engine.py` defines `score_gained` = «sum of the values of the tiles created
  by merges») already equals this real game score, so experiment aggregates
  (`score_max`, `P(score>=48k)`) are measured against the true leaderboard
  metric. `farm.go` may keep optimizing the S-sum *proxy* internally — that is
  a search heuristic, not a reported number, and does not affect the report.

## Open questions and how each will be answered

Measurement tooling: `bot2048 live research` (ResearchRunner,
`src/bot2048/runner/research.py`) plays cheap games and records every spawn
into the games DB; analysis via `bot2048 db` / SQL over the spawns table.

| # | Question | Default assumption | How we answer it |
|---|----------|--------------------|------------------|
| 1 | Spawn value distribution: P(2) vs P(4)? Anything else (8)? | classic 90% / 10% | research mode: diff engine-predicted board vs next screenshot after each move → `TileSpawned(exponent)`; ~1000+ spawns → empirical P(4) ±2–3% |
| 2 | Spawn cell distribution: uniform over empty cells? | uniform | same samples; chi-square of spawn cell vs empty-cell set per ply |
| 3 | Initial tiles: how many, which values? | **partially observed**: 2 tiles, mostly 2s, occasional 4 (see facts above); exact P(4) + cell uniformity still open | research mode records the freshly parsed board after «Нова гра» as ply −1/−2 `TileSpawned`; repeat over many games |
| 4 | Does the spawn distribution drift (by ply, by score, "rigged" anti-player spawns)? | no drift | per-ply/per-score buckets over the same data |
| 5 | Score formula: +merged-tile value per merge (classic)? | classic | **ANSWERED: classic cumulative** (proof below) |
| 6 | Animation timing: how long until the board is stable after a swipe? | ~350 ms (`settle_delay_s`) | screen-record via scrcpy, frame-step; also detected at runtime by parse-retry telemetry |
| 7 | Ads/popups: do they appear mid-game? What do they look like? | none seen yet | log every `ScreenState.POPUP` occurrence + screenshot dump during long research sessions |
| 8 | Board-full ≠ game-over handling: how does the app behave exactly with no legal moves (given the known bug)? | game keeps accepting input | deliberately reach a dead board in research mode; record app behavior; bot relies on its own `is_game_over` regardless |
| 9 | Timer above the board: does it affect gameplay (time limits, bonuses)? | cosmetic | observe long games; compare recorded outcomes vs timer |
| 10 | Swipe recognition thresholds (min distance/speed)? | any board-spanning swipe works | only if humanized swipes ever get dropped: telemetry flags "move not applied" retries per Humanizer parameters |

## Q5 proof — score is classic cumulative (manual spot-check, 2026-07-11)

Live capture of one in-progress game:

- Header **«Рахунок 25264»**.
- Board tiles: two 1024, one 512, one 256, one 128, two 64, one 16, two 8,
  two 4, three 2.

Three candidate metrics for that board:

1. **Board tile-value sum** (= engine `sSumBoard` proxy shorthand): `3118`.
2. **Classic cumulative score** (Σ merged-tile value over the whole game).
   Path-dependent, but if every leaf were a spawned-2 it has a fixed ceiling
   `Σ (r-1)·2^r = 25816` over this tile multiset. Each spawned **4** that ends
   up inside a tile replaces one `2+2→4` merge and so lowers the score by
   exactly 4 (downstream merges unchanged).
3. **Screen «Рахунок»**: `25264`.

`25264` is nowhere near the board tile-value sum `3118`, so the header is **not**
the S-sum / tile-value sum. It sits just below the all-2s classic ceiling
`25816`; the deficit `25816 − 25264 = 552 = 4 × 138` implies **138 spawned-4s**
incorporated into the current tiles. Combined with the board tile-value sum
(`3118 = 2·#2 + 4·#4`) that gives `1283` twos + `138` fours = `1421` spawns, i.e.
**P(4) = 138/1421 = 9.71 %** — matching the independently-measured
**P(4) = 9.77 %** (early-spawn research). Two unrelated measurements agreeing to
within 0.06 pp confirms the model: **score = classic cumulative (+value per
merge, spawns contribute 0).**

Consequence for the harness: `summary.final_score` is already this exact
quantity (`sim/runner.py` sums `engine.move().score_gained`, which `engine.py`
documents as "sum of the values of the tiles created by merges"), so no report
change is needed. The reviewer's concern #2 ("search optimizes S-sum, report
uses classic") is resolved as: the **report is the correct, real metric**; the
S-sum only lives inside `farm.go` as a search proxy.

## Outputs

- Measured spawn model → `bot2048.core.types.SpawnModel(exponent_probs, initial_tiles)`
  replacing the classic default in simulations, so sim results transfer to
  the real app.
- Timing constants → `LiveConfig.settle_delay_s`, humanizer delay tuning.
- Popup inventory → `vision.states` anchor patches.

## Safety constraints during research (same as live)

- Swipes only inside the safe zone (board bbox, mid-screen ≈ 25–75% height,
  clamped by `Humanizer`) — top of the screen is off-limits because of the
  notification shade.
- Taps only at calibrated coordinates («Нова гра», popup button).

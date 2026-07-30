---
name: cashout-and-champion-state
description: "monobank-2048 breakthrough state (2026-07-11 eve): cash-out +2048 confirmed live, tail-net champion behaviour, current build direction"
metadata:
  node_type: memory
  type: project
  originSessionId: 9994db1d-76a1-45df-a78d-0d55139499dc
---

State after the laptop restart on 2026-07-11 (evening), the session that found the first real lever.

**CASH-OUT CONFIRMED LIVE.** A deliberate 1024+1024→2048 merge DOES credit +2048 and ends the game
with that score counted. Andrii verified this on the device. This was Fable's #1 kill-test (the whole
route-α cash-out rested on an n=1 assumption). It is now settled — the intentional endgame cascade
(512+512→1024, then 1024+1024→2048) is a legal, scoring move. **This belongs in docs/rules-research.md
on the next commit** (not yet written there).

**Records to beat:** top-1 = 49,600, top-2 = 49,400 (real numbers from Andrii; 48k earlier was his
rounding). Likely HUMAN-set: ~1.2 s/move over ~51 min, well under the app's measured ~4.8 swipes/s
ceiling → limited by deliberation, not compute → the record is a STRATEGY, not deep search. Target
t* ≈ 2512 moves for 49,600 (score = Φ(final) − 0.4t, ~13.75 pts/move near the end). Higher target is
NOT better: every +1000 pts pushes t* deeper into the k=4 death window, cutting survival P. Aim
record + small margin, not max.

**The champion:** a tail-objective n-tuple TD net, λ=0.003, file `run/w-l0.003.bin` (backed up at
`tmp/champion-backup-2026-07-11/weights/champion-l0.003.bin`). Depth-4 paired arena, n=100, moves:
farm-d6 μ1677/max2065 → mean-d4 μ1837/max2223 → champion-d4 μ2021/median2067/max2409. Tail training
isolates ~+184 μ over the same-architecture mean net. First lever ever to move the whole distribution
(median ≈ prod's old max), not just μ. n≥400 confirmation was running at handoff.

**KEY DIAGNOSTIC (how champion games END, k1024 = #1024-tiles at death):**
champion dies at k=4 in **49%** of games (farm: 9%) and dies ON a forced 1024+1024 merge **22%** of
the time (farm: 2%). So the net did NOT learn the gate's "hold and delay" strategy — it does the
OPPOSITE: pushes aggressively to the 4th reservoir and already reaches the cash-out event, but
UNINTENTIONALLY (merges when forced, at non-maximal score_now). Implication: gate and net do different
things (not redundant); the champion already reaches the endgame and just "presses the button" at the
wrong moment. Leading hypothesis for the #1 build: add a cash-out TRIGGER on top of the existing
champion (fire the 2048 merge when score_now is maximal + board densest), BEFORE retraining or gating.
Fable was asked to confirm/deny this direction (Q2). See [[poc-then-optimize]], [[2048-simulation-discipline]].

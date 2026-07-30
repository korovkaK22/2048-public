---
name: poc-then-optimize
description: "Working agreement for monobank-2048 experiments — small proof-of-concept runs first (≤20 min), full optimization only after a chance is confirmed"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 9994db1d-76a1-45df-a78d-0d55139499dc
---

Andrii's working rule (2026-07-11), set right after a 2.5-hour arena had to be killed mid-run:
**experiments run as a small proof-of-concept first, ≤20 min total wall-clock, to feel out whether
there's a chance at all. Only once a PoC shows the wall can move do we spend on full optimization
(n≥400 arenas, retraining).**

**Why:** the expensive failure mode in this project is running a full n=100–400 campaign on a lever
that was never going to work. A structural change either moves the truncation wall or it doesn't —
that's a binary, large (+300–400 max) signal visible at n≈30–40. Burning hours to rank a dead lever
is pure waste.

**How to apply:**
- A PoC targets a **binary "did the wall move"** verdict, NOT a μ/σ ranking. This is the one case
  where small n is legitimate despite [[2048-simulation-discipline]]'s "n=12 is not data" — that
  rule is about 50–200-move μ differences (needs n≥400); a merge-gate's expected effect is +300–400
  to *max*, which shows up at n≈40. Different question, different sample size.
- Concrete PoC gate example — **merge-gate at root**: n≈40, depth-6, ~15 min, PASS ⟺ ≥1 game > 2300
  (ideally a cluster near 2450); FAIL ⟺ max stays ~2100.
- The cash-out PoC is NOT a simulation — it's ONE live game on the phone (does a deliberate
  512→1024→2048 cascade credit +2048 and end with that score?). Cheap, decisive, do it first.
- PoC budget goes ONLY to things that can move `max`. Do not re-measure closed questions (search
  depth, cprob horizon, eval-term risk — Fable rejected eval terms as the wrong shape).
- Respect the one-game-runner rule: don't start a PoC while another arena/ntupleT run is live.

See [[2048-simulation-discipline]] for the CPU/sample-size mechanics this builds on.

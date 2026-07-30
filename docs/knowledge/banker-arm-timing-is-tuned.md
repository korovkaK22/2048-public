---
name: banker-arm-timing-is-tuned
description: "Lowering the banker's arm/target to secure the 48500 record floor DESTROYS top-3 — late arm (49901) is near-optimal"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 9994db1d-76a1-45df-a78d-0d55139499dc
---

Andrii's record bar is 48500 (any bank >48500 = record; 49k≈top-10; 50k=top-3, the goal). Natural idea: lower the banker's arm so boards peaking in the 48500–49900 "dead band" (e.g. phone game f9ab4476: peak_potential 49552 → banked 45612) get secured instead of jamming. **Tested both ways on the 10 reacher seeds (2026-07-12) — BOTH catastrophic:**

- Lower solver TARGET 49601→48500: 2/10 obliterated (230210 51024→44404, 230250 50536→44984). Solver *satisfices* at the lower bar and dumps the upside.
- Lower arm only (TARGET=49601 kept, EARLYFIRE=-800 → arm≈48801): 9/10 regressed -1352…-5116. Early arm *caps* the board near the arm point (~47-48k) because the wall net hasn't built it to 51k yet.

**Why the current LATE arm (cashValue≥49901) is right:** it lets the reach-net (wall) build the board to its peak BEFORE the banker takes over, so the maximiser banks the 50-51k overshoot (top-3), not 49k. It already "holds for the higher hand" — exactly what Andrii wanted. It does NOT cut early.

**Why:** early handoff to the banker sacrifices the ceiling to secure a floor — a bad trade (proven 5x now with today's two). Confirms [[soft-not-hard-overrides]] (hard/early banking hurts) and the reach-limited reality [[numbers-must-come-from-data]].

**How to apply:** do NOT lower the banker target or arm. The only theoretically-safe dead-band fix is a true PEAK-DETECTOR (bank on cashValue *descent*, not a threshold) — but it's risky (noisy cashValue → false peaks cap high games) for a rare payoff while REACH is the binding constraint. Keep target=49601, earlyfire=300, earlyEmpties=2. Spend time playing, not re-tuning the arm.

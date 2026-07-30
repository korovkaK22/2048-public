---
name: theoretical-max-score
description: "Correct theoretical-max score for the taboo 2048 clone: 106,496 legality / ~81k achievability — NOT 51k or 100k (both were wrong)"
metadata:
  node_type: memory
  type: reference
  originSessionId: 9994db1d-76a1-45df-a78d-0d55139499dc
---

Derived rigorously 2026-07-21 (4 parallel agents: rules + DP + constructive + Fable). The competitor top-1 hit **58k**, which forced a strict re-derivation. **Two earlier numbers I quoted were WRONG — never repeat them:**
- **51,529** — WRONG. That's the max for a board with ONE 1024 (classic monotone snake). Not the ceiling.
- **100,262** (docs/audit-2026-07-11.md §3.2) — WRONG. Arithmetic slip: mixed a legality bound with an expected 4-penalty on a sub-optimal board. Also its whole per-k table (44245, 58813, …) is not divisible by 4, so cannot be exact scores.

**Rules (confirmed):** forming 2048 = terminal WIN (popup «Ви зібрали 2048!» → only «Нова гра»; cash-out +2048 is just the merge value, not a separate bonus). So **max tile is capped at 1024**. No app move/time limit (timer cosmetic). This 1024-cap is the SINGLE hard ceiling: if 2048 didn't end the game, max would jump to 237,568 (cap 2048) / 524,288 (cap 4096).

**Score identity (exact, validated on the real engine):** score = Σ_tiles (r−1)·2^r − 4·N₄, where tile = 2^r and N₄ = absorbed 4-spawns. Every real score is divisible by 4.

**The correct ceilings:**
- **Legality max = 106,496** — 8×1024 + 8×512 checkerboard. Proven optimal (grid is bipartite 8+8; ≤8 cells can be 1024). maxΦ(k 1024-tiles) = 47,104 + 7,424·k. Two-1024 max = 61,952 (not 58,813).
- **Achievability max ≈ 80,864** (perfect rigged luck) / 71,684 (luck-free). You can freeze at most 7–8 dead 1024s; the leftover cells collapse to ONE descending ladder 512+256+…+4 (Φ 7,172), NOT eight more 512s — dead 1024 mass eats the workspace. If sliding-geometry can't safely hold 8 frozen 1024s → drops toward ~62k (6×1024). This geometry point is unresolved (a naive rigged planner only reached ~19k in sim — weak lower bound, not the true achievable).

**Practical takeaway (the point):** the competitor's 58k is ~2 big tiles, ~72% of the achievability ceiling — **NOT combinatorially blocked**. The gap is pure SURVIVAL / game length: 58k ≈ 3,187 moves; 70k ≈ 3,846; bot record ~2,108 moves. Density and length are ONE variable (each extra 512/1024 = +768 mass = hundreds of moves). No combinatorial trick exists (see [[numbers-must-come-from-data]], [[banker-arm-timing-is-tuned]]). To beat 58k the ONLY lever is surviving ~1,000+ more moves before board combinatorics matter. Rigged spawns exist only in sim; live is random-spawn, so the real ceiling is the hazard/survival frontier we already measure.

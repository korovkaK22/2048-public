---
name: beat-58k-final-closure
description: "FINAL closure of the beat-58k research (2026-07-25): all autonomous sim levers exhausted, aggregate-leaderboard hypothesis killed logically, human-imitation (PoC-B) dead (user won't play), banker-ON tail confirmed the ~51.1k single-game wall over n=8220. Nothing left to test autonomously; only remaining lever is throughput (more live attempts), an ops decision not a research one."
metadata: 
  node_type: memory
  type: project
  originSessionId: 9994db1d-76a1-45df-a78d-0d55139499dc
---

On 2026-07-25 the beat-58k effort reached exhaustive closure. Andrii said the human-imitation lever is dead ("нема ходів і не буде, самі тестим" → then "стопай що йде, підбивай підсумки"). Everything was stopped, cron faa128c2 deleted, caffeinate reverted.

**Final state — every door closed:**
- **New sim ideas: CLOSED.** Fable (2026-07-25) found no orthogonal lever with prior >5% outside the CLOSED list. Any lever hits one of two exits — survival-policy (all RED; two independent inits converge to the same TD fixed point = champion) or 4×4 fill-pressure geometry (physics, not engine sub-optimality). Both exhausted. See [[beat-58k-via-k4-cascade]] for the 10+2 proofs.
- **Aggregate-leaderboard hypothesis: KILLED logically.** The competitor's 58k could in principle be a cumulative/sum-of-games score rather than one game — which would moot the whole single-game ceiling. Falsified WITHOUT the competitor's table: our own leaderboard row is ~49.6k (a plausible single champion game, sim-max 51.1k). We've played hundreds of live games; a cumulative row would be in the millions (263 × ~38k ≈ 10M), not 49.6k. Plus: the tight 53-58k cluster among ~10 people is the order-statistic signature of a per-game distribution over human volume, not cumulative; and cash-out +2048 terminates the game = "one banked game = one record" semantics. → 58k = ~3135 moves in ONE game = +22% over champion max 2569 = the proven fill-pressure wall.
- **PoC-B human-imitation: DEAD.** User will not play; no human move data will ever come.
- **Banker-ON tail (the last live measurement): wall CONFIRMED.** n=8220 games, banker-ON maxcash exact config (same knobs as champdist_400). max=51,128 (frozen — did not move a single point from n=2880 to n=8220), p99.9=51,092, ≥52k=0, ≥53k=0. Top-6 games all in a 32-point band at ~51,100. Second independent metric giving the same ~51-52k wall as the 52,212-achievability analysis. Stopped early by user at n=8220 (already decisive; 10k target unnecessary).

**Bottom line:** the engine provably walls at ~51-52k single-game. The only remaining path to 58k is NOT engine strength but **throughput** — run many more live attempts to fish the rare (~1e-4) support-bearing board where the frozen champion happens to bank 52k+ (R3-exec showed such boards finalize at mean 55.8k / max 63.6k, but natural arrival is ~0). That is an operational/ops decision, not a research one. See [[cashout-and-champion-state]], [[theoretical-max-score]], [[games-continue-infra-is-fine]].

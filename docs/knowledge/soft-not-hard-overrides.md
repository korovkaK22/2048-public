---
name: soft-not-hard-overrides
description: "monobank-2048: soft leaf penalties help the champion net, HARD move-vetoes/risk-backup overrides hurt it — proven 3x"
metadata:
  node_type: memory
  type: project
  originSessionId: 9994db1d-76a1-45df-a78d-0d55139499dc
---

Confirmed 2026-07-11 by paired n=50 A/B on the champion net (EXP-utility λ=0.003):

**Soft leaf nudges the value net can OVERRULE help. Hard overrides that SUPPRESS the net hurt.**

Evidence (all paired, same seeds):
- **Soft adjacency-1024 penalty (leaf feature, W=100): WORKS** — median 2067→2108 (+41), forced_2048
  22%→15%, adj-exposure −35%, survival intact. The net can outvote it.
- **Hard "safety bundle" (Fable's #1: adversarial DFS root-move veto + min-mix risk-averse chance
  backup at k≥3 + hazard cash-out, gated k≥3 & empties≤5): FAILS** — median 2067→1982 (−85),
  max 2409→2236, bestScore 47812→42292 (−5520). Everything worse. This was the exact single
  decisive test Fable designed to falsify his own thesis, and it falsified it.
- Consistent with the earlier `kappa` search-time risk override (1655→1252→752→345 as α ramped)
  and the phase-policy research: forcing risk-aversion / vetoing the net's moves near k=4 makes the
  already-tuned aggressive champion play timid and die earlier.

**Rule going forward:** improve the champion with SOFT, tunable, net-overrulable leaf features
(and via retraining the objective), NOT with hard root vetoes or risk-averse backup surgery. The
net is at a tuned optimum; overriding it is negative EV.

Separately, the real gap to the record is **cash-out TIMING, not survival**: with target=49600 the
cash-out only fires forced (nSurvive==0, board jammed, nothing left to bank), so best score capped at
47812 (+2048 from the single forced merge). The bridge to 49600 is firing the cash-out EARLIER (with
free cells left) so the endgame maximizer can bank multiple merges before the terminal 2048.
See [[cashout-and-champion-state]], [[poc-then-optimize]].

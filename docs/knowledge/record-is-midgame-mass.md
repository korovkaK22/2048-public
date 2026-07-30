---
name: record-is-midgame-mass
description: "monobank-2048: RECORD PROVEN REACHABLE in-sim (2026-07-12). A two-term 'wall' retrain (fine-tune from champion: reward DENSE k=4 + penalize k=5) banked 50284 & 50860 (>49600 record) via deliberate solver cashout — champion was 0/240 impossible. Rate ~0.3% (rare). Next lever = phase-gate the shaping to lift the rate. mass/doom/EV-fire/anti-k5-leaf all failed."
metadata:
  node_type: memory
  type: project
  originSessionId: 9994db1d-76a1-45df-a78d-0d55139499dc
---

**FRESH-HOLDOUT VALIDATED + TAKEN AS DEPLOY NET (2026-07-12, decisive).** The wall net
(champion-wall-snap-0316.bin, md5 b030c9664436043fd4ae769e7aebb9e2) + earlyfire, run on a FRESH
holdout (seeds 230000+, n=225, NOT curated): realized record (banked>=49601) = **2/225 = 0.89%**,
TRUE reach (peak_pot>=49601) = **4/225 = 1.78%**, max banked 51024, surrogate reach>=47k 5.33% /
>=45k 24.44%. This END-TO-END confirms the reach×bank projection (~0.73%) on unseen seeds — it is the
strongest config we have PROVEN. Andrii's call ("take what's VERIFIED strongest, not the unproven
retrain"): the phase-gate retrain was NOT launched (it might not beat 0.89%); we TOOK the wall net.
PRESERVED: until now it lived only in gitignored tmp/ (whole record capability on one un-backed file);
now committed to git at project/tools/ntuple/run/champion-wall-snap-0316.bin on branch
feat/live-record-engine (tip 4b4252f, code+net = self-contained deploy unit), pushed to origin, local
mirror artifacts-record-net/. Weights channel = force-add past the run/ ignore, same as the champion on
origin/feat/ntuple-champion. DEPLOY: BOT2048_RECORD_ENGINE=1 -weights run/champion-wall-snap-0316.bin
depth 4 EMPTIES=8 NODES=6e6 PCONF=0.6 EARLYFIRE=300 EARLY_EMPTIES=1. All-in = accept −1683 median for
max P(>=T). Bridge port (serve.go, 08b5c7c) is bit-identical to arena on champion; the 50284/200280
"non-repro" the bridge agent saw was a NET MISMATCH (it loaded champion, record needs the wall net).

Refines the "gap is cash-out TIMING" framing in [[cashout-and-champion-state]]. Both endgame AND mass
are live levers — this corrects an over-conclusion from a too-small smoke.

**KEY (n=50, verify-numbers lesson):** Fable's peak record-potential pre-check (per game, max over
trajectory of `score_t + cashValue(board_t)` = best final score if you flip to a perfect endgame
maximizer at the best moment). The n=5 SMOKE showed max 45.7k / 0 crossings and I wrongly concluded
"endgame is dead." The **n=50** run showed the real tail: **p95=47.8k, p99=49.2k, max=50.3k, and 2%
of games have peak-potential ≥ 49,601** (8% ≥ 47k, 26% ≥ 45k). So the record IS reachable on ~1 in 50
champion games — n=5 was far too small to see a 2% tail ([[2048-simulation-discipline]]: n is not data
for a rare tail; [[numbers-must-come-from-data]]). peak_potential is an UPPER bound (assumes recognizing
the peak moment + perfect banking), so realized rate is lower, but non-zero and real.

**RECORD NOT REACHABLE ON CURRENT CHAMPION — PROVEN (2026-07-12 night):** record-hunt (champion+
cashout+solver, 240 fresh games) max banked 47464, ZERO ≥49601; solver-bank falsifier 0/8 top-potential
seeds; the one feasible seed (1022, pot 50253) intractable for -exact even @60M nodes (652-pt margin =
near-perfect endgame, tree too deep). Champion+cashout is MAXED at ~47.5k. STOP: attempts-trawls, solver
node-cap raises, search-time overrides — all three proven dead.

**THE BINDING CONSTRAINT = HEADROOM = wall × density, and they DECOUPLE past the 2409 wall.** The
Rosetta-stone game survived 2477 moves (past the wall) but banked only 46116 (BELOW the 240-game max):
past 2409 you survive only by SHEDDING density (thin 512s, avoiding k=5-forcing merges). So corr(moves,
potential)=0.988 held only because density was ~constant in-policy; at the tail length and density
anti-correlate. The record needs BOTH ~2500 moves AND a dense board that cascades clean to +2048.

**THE ONE LIVE LEVER = a TWO-TERM retrain** (Fable, decisive): penalize entering k=5 AND reward DENSE
high-support k=4 (rich 512/256 under the four 1024s) at length — NOT k=4 emptiness. Pure MASS FAILED
(burned survival, median 1752→1550 = dominated). Pure anti-k5/survival reproduces the plateau or makes
thin long games. A SOFT leaf term can't move a trained plateau (~+40-move nudge; need +100 AND a density
shift) → must be a RETRAIN, fine-tuned FROM the champion (4h window). Build `-target wall` mode
(tmp/retrain-prep). Morning falsifiers (paired n=50 vs champion): (1) games past 2409; (2) THE one that
matters — games banked ≥48k with median banked not below champion; (3) kill if training median drops −200
like mass (k4-penalty too hard, starving mobility). 49601 odds ~30-40% via retrain = ragged edge, NOT
architecture-bound (arch already made a 50253 board; record is human-set = strategy provably exists;
bigger tuples = fallback only).

**RESULT (2026-07-12 ~04:00): the two-term wall retrain SUCCEEDED.** Fine-tuned from champion (`ntupleRetrain.wall
train -target wall -init w-l0.003.bin`, walldensew=80 wallk5w=150), training median rose 1520→1767 then settled
~1590 (the intended density-for-length trade; NOT the mass collapse). The median-1587 snapshot
(`tmp/retrain-prep/ntuple/run/ab/champion-wall-snap-0316.bin`) + cashout+exact solver banked **50284 (fresh seed
200280) and 50860 (seed 5016)** — both > the 49,600 record, `forced_2048=False reached_target=True` = deliberate
solver cashouts. Champion+cashout = 0 records / 370 games, ceiling 47,464. Wall-net rate ≈ **0.3%** (1/570 fresh +
1/20 diag) = reachable but RARE; it trades median score (−1683 paired) for the record tail (correct, record = max
P(≥T) not E[score]). **NEXT LEVER (untried) = PHASE-GATE the shaping** (`ntupleRetrain.wallgate -wallgate`, set
BOT2048_WALL_GATE_EMPTIES=3 since empties≤6 fires 75% of moves; the tight gate is k≥3): keep early-game mobility
(pure +1/move survival) + density ONLY in the endgame → reach the deep dense state more often → lift the 0.3% rate.
Warm-start the gate from the CHAMPION (clean mobility), not the eroded wall snapshot. Record seeds: 200280, 5016.

**BANKING IS THE LEAKY BUCKET, not reach (Fable, 2026-07-12, decisive reframe).** Decompose
P(record) = P(reach ≥49.6k-potential board) × P(solver banks | reached). Measured: reach ≈ 0.9%
(16/1810 hit potential≥49601) but realized ≈ 0.06–0.3% → **banking efficiency only ~1/16**. So
70–90% of already-reached record boards are THROWN AWAY at the banking step. Near-miss proof: seed
200203 reached peak_potential 51914, banked 48600; 201253 reached 52146, banked 46492 — the boards
were there, the solver mis-banked. CRITICAL: "depth is anti-tail" applies to the NET's reach-phase
expectimax (1-ply-trained → deep search compounds error), NOT to the target-conditioned endgame
SOLVER (a certified banker, not an approximator) — so pouring compute into the ENDGAME solver is pure
banking upside, and a fast 14-core Mac (prod is moving onto it) feeds exactly that. **Highest-leverage
12h lever = MAX BANKING on the 16 known reacher seeds** (n=16 = DENSE cheap signal, not a rare hunt):
sweep EARLIER fire-timing (fire with free cells left so the maximizer banks multiple merges, not just
the forced terminal +2048) + higher EXACT_NODES + EXACT_EMPTIES/TARGET_PCONF. If even 3–4/16 bank,
realized rate goes 1/1810 → ~4/1810 with the net we ALREADY have — no retrain. Winner-shaping retrain
= #2 (lifts reach). Wider tuples = killed for this window. Measure everything at a SURROGATE threshold
(peak_potential≥47k ≈ 8%, ≥45k ≈ 26% — measurable at n=200–400); raw record count is NEVER the metric
again (0.1% invisible at any affordable n). Prod on Mac (ARM) → evalMLP OFF (x86-only win).

**BANKING LEAK FIXED — 0/16 → 13/16 (2026-07-12, CONFIRMED on the 16 reacher seeds).** Fable's thesis
proven with live numbers. The exact solver's endgame is INTRACTABLE at record margins (hits the node cap
even at 20M/60M, returns P=0) so it never fires at the high-potential moment and defers to the net until
the board JAMS → banks only the forced +2048 (~46-49k). FIX = the `BOT2048_EXACT_EARLYFIRE` heuristic
fire (agent-added knob in tmp/exact-build exact.go/search.go): fire the score-maximiser directly (NOT
waiting for the intractable exact P-confirmation) once `pot >= target + EARLYFIRE` AND `countEmpty >=
EXACT_EARLY_EMPTIES`, inside the solver-engaged window `[EARLY_EMPTIES, EXACT_EMPTIES]`.
KEY MISTAKE→FIX: potential and density are COUPLED — record potential (~50-52k) only appears when the
board is already dense (<5 empties). So EARLY_EMPTIES=5 (my first arm) → window [5,8] never sees pot≥50k
→ bit-identical to baseline (0 change). Lowering to **EARLY_EMPTIES=1, EARLYFIRE=300** (fire at pot≥49901
with ≥1 free cell, window [1,8]) → **13/16 bank ≥49601, max 53000** (champion-wall-snap-0316.bin, depth 4,
NODES=6M, EMPTIES=8, PCONF=0.6). Banking factor jumps ~1/16 → ~13/16 (81%). Realized record rate ≈ reach
0.9% × bank 81% ≈ **0.73% (~12× the old 0.06%)**, NET UNCHANGED, no retrain. earlyfire is SELF-GATING
(fires only at pot≥49901 = near-record boards ≈0.9% of games) so it is inert on normal play — VALIDATED:
paired 30 normal seeds off vs on = BIT-IDENTICAL (median 38952, mean 35750, max 43852, 0/30 games changed).
So it is a CLEAN, SAFE, DEPLOYABLE win. Bank is no longer the bottleneck; REACH (~0.9%) via winner-shaping
is now lever #1. The live cutover/bridge economics flip from "premature" to "the deploy path": port
earlyfire+exact+cashout into project/tools/ntuple/serve.go so the phone can bank — build it once reach is
lifted to grind-able (~2-4%) and the combined rate is confirmed at n≥400 on the surrogate threshold.
Repro: seed 200280 = 50284 at depth 4 EXACTLY (determinism intact; the agent's earlyfire edit is safe).

**PROD VALIDATION (2026-07-12): the banking leak is REAL ON HARDWARE.** Our best live prod game
(c0195fe4, budget-250 net, banked 48136, 2432 moves) had computed **peak_potential = 50234** (at ply 2423,
score 46008 + cashValue 4226; board k1024=5 THIN support, mass 5334) — ABOVE the 49601 record. It banked
only 48136, leaking 2098. So earlyfire would have made our BEST REAL GAME a 50234 record. Prod already
builds record-capable boards; we just don't bank them. (Computed by replaying the DB moves' board_after +
score_total through the arena's cashValue via a scratchpad `potential` subcommand — needs initTables().)

**POTENTIAL-CONDITIONED 3-WAY POLICY (Andrii's idea, rational — being implemented in the bridge):** compute
achievable potential live (cashValue). BANK (solver+earlyfire) when pot≥~49k; ABANDON when pot<~47k (fresh
attempt beats grinding a doomed endgame); SALVAGE (risk-seeking, e.g. the kappa knob) in the ~47-49k band —
because a doomed game's safe play yields a worthless non-record, so variance is FREE. This does NOT contradict
[[soft-not-hard-overrides]]'s "risk-seeking hurts" — that was GLOBAL kappa (hurting games that WOULD reach);
gated to the doomed regime the downside is irrelevant. UNMEASURED though: must test that conditioned kappa
actually spikes potential over 49.6k rather than just dying faster (which is fine, = abandon). Bridge builds
the 3-way gate framework (salvage=kappa, flag-gated, default-off); the kappa value + thresholds get tuned by
a follow-up lane test before going live.

**What FAILED (paired n=50, same seeds):**
- **doom-oracle** (adversarial survival gating cash-out): DEAD — fires too early (96% vs 26%), median
  40088→34824 (−5264), max 47812→41192. Adversarial spawn model wrong-shaped (spawns are random).
- **EV-fire** (E-based `19.68·E[moves] < cashValue`): median +1000 but **max −2536** (kills the tail).
  E-maximization is wrong for a threshold record → use P(≥T). This is WHY the solver must be
  target-conditioned, not E-based.

**perf #1 (net-leaf MLP gather split) — NEGATIVE on Apple M4 Pro** (leaf already ~270ns; wide OoO hides
the 64 loads; the 660-816ns premise was x86). Regressed 8-15%. Kept champion eval; `evalMLP` retained
BIT-EXACT behind a flag for x86 prod (user's prod is x86 — the split should help there). Follow-up #2 =
int8 quantize (14MB fits L2, attacks miss RATE) — lossy, needs arena PoC.

Per-move search parallelism does NOT exist (only arena `-workers` across games). This Mac is TEST-ONLY;
prod (live game) is a separate x86 machine. See [[soft-not-hard-overrides]], [[poc-then-optimize]].

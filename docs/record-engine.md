# Live record engine (bridge) — flags, routing, how to enable

The live serve engine (`project/tools/ntuple`, driven by `go_engine.py`) reaches
near-record boards but, by default, plays the survival/farm net all the way — it
has no machinery to deliberately BANK the 49,601 record. This bridge ports the
offline arena's record-banking stack into the live engine, **flag-gated and
default-OFF**: with the master flag unset the engine is byte-for-byte the golden
engine (evalMLP + HEUR5-off + budget ladder + prefetch + leaf-cache); with it on,
the ENDGAME is decided by a certified banker instead of the net's deep search.

All the ported machinery lives in `record.go`; the serve/arena wiring is a small,
gated addition to `serve.go` and `main.go`.

## What the record engine does

In the dense endgame it replaces the net's anti-tail deep cprob-ladder search
(measured depth-5 = 0/16 banked vs depth-4 13/16) with:

1. **Target-conditioned solver** (`exactSolver`) — expectimax whose terminal
   value is the success INDICATOR `1[final >= T]`, so `V(board) = P(final >= T |
   optimal play)`. Once engaged it OWNS move selection (P-argmax, `E[banked]`
   tie-break) and fires the game-ending 1024+1024→2048 itself when that is
   optimal. A thin-margin irreversible fire is gated on `P* >= pconf`.
2. **Endgame score-maximiser** (`scoreSearcher`) — banks every beneficial merge
   and does the 2048 merge LAST, maximising the expected final banked score.
3. **Early-fire** — once the maximiser already expects to bank `>= T + earlyFire`
   and free cells remain, the position is handed to the maximiser NOW rather than
   drifting to a jam and banking the merges too late. This is the lever that
   turns 0/16 into 13/16.

The banking decision is a **fixed-node** solve + **fixed-depth** score search —
no wall-clock branch anywhere — so the endgame move is deterministic and
reproducible regardless of think time. This is the "budget feeds the certified
banker, not the net's deep search" routing: in the endgame the serve path calls
the solver in place of the time-bounded budget ladder.

## Flags (env vars)

| Env var | Default | Meaning |
|---|---|---|
| `BOT2048_RECORD_ENGINE` | `0` (off) | **Master switch.** `1`/`on`/`true`/`yes` enables the whole stack. Off ⇒ golden engine, byte-identical. |
| `BOT2048_TARGET` | `49601` | Success threshold T (strict `>=`). |
| `BOT2048_EXACT_EMPTIES` | `5` | Region gate: solve only when `countEmpty(b) <= this`. |
| `BOT2048_EXACT_NODES` | `3000000` | Max-node expansion cap per solve (conservative: on exceed returns P=0). |
| `BOT2048_TARGET_PCONF` | `0.95` | Min `P*` to enter a thin-margin irreversible 2048 fire. |
| `BOT2048_MAXCASH_DEPTH` | `8` | Depth of the score-objective search (maximiser + tie-break). |
| `BOT2048_EXACT_EARLYFIRE` | `0` (off) | Early maximiser-fire margin (points): fire once `pot >= T + this`. |
| `BOT2048_EXACT_EARLY_EMPTIES` | `1` | Min free cells for an early fire. |
| `BOT2048_SALVAGE_KAPPA` | `0` (no-op) | **Reserved, inert stub.** Not wired to any search path in v1. |

### Validated winning config (13/16 reacher seeds banked; 0/16 without earlyfire)

```
BOT2048_RECORD_ENGINE=1 BOT2048_TARGET=49601 BOT2048_EXACT_EMPTIES=8 \
BOT2048_EXACT_NODES=6000000 BOT2048_TARGET_PCONF=0.6 \
BOT2048_EXACT_EARLYFIRE=300 BOT2048_EXACT_EARLY_EMPTIES=1
```

## Routing (serve path)

`handleSearch` (mode-0 / learned net only; farm mode-1 untouched):

```
if record engine ON and countEmpty(b) <= EXACT_EMPTIES and ceiling(b) >= T:
    -> recordSearch: solver (+ earlyfire) picks the move   [depth_limit marker
                                                             990=solver, 991=earlyfire]
else (or solver vetoes a thin-margin fire):
    -> golden budget-ladder net search (unchanged)
```

`recordSearch` builds a fresh `exactSolver` per request (memo + node budget
reset, so every move gets the full nodeCap) and delegates to `recordDecision`.
The serve wire reply keeps its 4-field contract `<dir> <value> <ms>
<depth_limit>`; the `depth_limit` field carries the banker marker (990/991) as
opaque int metadata the client already tolerates.

## Telemetry (SQLite audit) — `features` command

New serve command, works with the engine on OR off (pure board math + one score
search):

```
features <16hex>
  -> features board=<hex> k1024=<n> empty=<n> k512=<n> k256=<n> support=<n>
     ssum=<f> ceiling=<f> pot=<f> target=<f> engaged=<0|1> earlyfire=<0|1>
```

`pot` = `cashValue(b)` (the maximiser's optimistic estimate of the final banked
score); `engaged`/`earlyfire` say whether the bank path would drive/early-fire on
this board. The runner issues it at the endgame-entry checkpoint and records the
line, so every endgame decision — most importantly which boards the banker
engaged — is auditable postfactum in the SQLite pipeline.

**v1 ships NO abandonment gate.** A ROC analysis over 200 fresh seeds showed
doomed and record-reacher games are statistically indistinguishable in every
endgame checkpoint feature (k1024, support, score, provable-ceiling) until the
last ~40–120 plies — after the compute is already spent — and that the "provable"
ceiling is not a valid whole-game bound (future spawns add mass; the real record
game had `ceiling < T` until ply ~2275). So `features` is telemetry only; it
makes no decision. `salvage` is likewise a flag-gated no-op (analysis:
salvage competes with a fresh cheap attempt, not with safe play, so the optimal
policy collapses to bank/abandon and abandon does not work).

## How to enable live

Set the master flag (and the winning levers) in the environment the runner uses
to spawn the engine (the engine reads them at startup and reports them via
`version`):

```
export BOT2048_RECORD_ENGINE=1 BOT2048_TARGET=49601 BOT2048_EXACT_EMPTIES=8 \
       BOT2048_EXACT_NODES=6000000 BOT2048_TARGET_PCONF=0.6 \
       BOT2048_EXACT_EARLYFIRE=300 BOT2048_EXACT_EARLY_EMPTIES=1
```

`version` then reports `BOT2048_RECORD_ENGINE=1` plus every lever, so a live run's
provenance records exactly which banker config played.

## Reproduction / validation

Batch validation reuses the same machinery through `arena` (self-play with a
simulated rng; the rng/spawn/board helpers are byte-identical to the arena, so a
seed reproduces the arena trajectory exactly).

**Which net matters.** The banking MACHINERY (solver/maximiser) is net-independent
— it is pure board arithmetic — but the SURVIVAL trajectory that reaches a
record-capable endgame is not. The default champion net (`run/w-l0.003.bin`, the
tail-net λ=0.003) maxes out at ~47.5k and banks 0/240 records, so it never feeds
the banker a ≥49601 board. Records reproduce only under the **WALL net**
(`champion-wall-snap-0316.bin`) at **depth 4**:

```
BOT2048_TARGET=49601 BOT2048_EXACT_EMPTIES=8 BOT2048_EXACT_NODES=6000000 \
BOT2048_TARGET_PCONF=0.6 BOT2048_EXACT_EARLYFIRE=300 BOT2048_EXACT_EARLY_EMPTIES=1 \
./ntuple arena -weights <path>/champion-wall-snap-0316.bin -leaf ntuple -depth 4 \
  -games 16 -seed0 <seed> -workers 8 -cashout -maxcashout -exact
```

- **16-reacher parity set** (WALL net, depth 4 — 13 bank ≥49601, 3 miss):
  `200025 200031 200187 200203 200236 200550 200604 200713 200778 201005 201118
  201186 201214 201253 201584 201699` (misses: 200187, 201214, 201699).
- **Bit-identical to the arena (verified):** on the *champion* net, seed 200280
  gives 41576 (not cashed) in BOTH the offline arena binary and this live port —
  identical to the ply. That proves the port is a faithful copy; swapping in the
  WALL net is what turns 41576 → the 50284 record (arena `bank16-base` shows the
  same seed/depth banks 50284 under the WALL net).
- **Focused unit test** (`record_test.go`, net-independent): the ported solver
  rates a dense reacher board `exactP == 1`, the maximiser's `runEndgame` actually
  banks a final `>= 49601`, and `recordDecision` engages and returns a legal
  banking move — while a zero (off) config never engages.
- **Live serve path:** a `search` on a dense reacher board with the engine ON
  returns the banking move directly (banked 50688 ≥ 49601 in the smoke), tagged
  with the solver/earlyfire marker in the reply's `depth_limit` field.

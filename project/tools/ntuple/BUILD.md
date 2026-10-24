# Build & launch note — ntuple engine (perf stack)

## Production build (champion, optimized)

**macOS arm64 (Mac mini M4 — the live host):**

```sh
cd project/tools/ntuple
go build -pgo=auto -o ntuple .          # no GOAMD64: that flag is amd64-only
```

**Windows / Linux amd64 (i7 box):**

```sh
GOAMD64=v3 go build -pgo=auto -o ntuple.exe .   # ntuple on Linux
```

- `-pgo=auto` picks up `default.pgo` in this directory automatically (Go ≥1.21;
  we are on 1.24). The profile is an endgame CPU profile of the search leaf.
  `default.pgo` is **gitignored** and was recorded on x86: without the file
  `-pgo=auto` is a silent no-op, and an x86 profile applied to an arm64 build
  only guides inlining (harmless but not tuned). Regenerate it **per arch**
  with the `perfbench -cpuprofile` command below.
- `GOAMD64=v3` requires a Haswell-or-newer x86-64 CPU (POPCNT/BMI2/AVX2); on a
  pre-Haswell CPU drop it. Irrelevant on arm64.
- `scripts/macos/setup.sh` runs the arm64 build for both engines.

### Bit-exactness: within one architecture only

PGO and `GOAMD64` are bit-exact **on amd64**: Go's amd64 backend never fuses
multiply-adds, so build flags change layout/inlining but not a single result.

**Across architectures this does NOT hold.** Go's arm64 backend emits fused
FMA (`FMADD`/`FMSUB`) for `a*b + c` patterns — one rounding instead of two — so
the net's float64 leaf sums differ in the last bits, and in near-tie positions
the chosen move differs. Consequences:

- `perfbench -workers 1` prints a **different `checksum=`** on arm64 (M4, M4 Pro)
  than on the i7. That is expected, not a regression. Compare checksums only
  between builds of the **same GOARCH** (two arm64 builds, or cache on/off on
  one machine, MUST match).
- The arm64 reference checksum is the M4 Pro's (same code path as the M4).
  `docs/research/arm_20core_scaling.md` and older notes that call the i7
  checksum "the" bit-identity gate mean amd64 only.
- Statistically (median, tail) the two architectures play the same engine;
  individual games from the same seed can diverge after the first near-tie.

## Launch (live)

The live runner spawns this binary through `scripts/live.sh`, which sets:

```sh
BOT2048_ENGINE2048_BIN=tools/ntuple/ntuple          # the live net engine, not engine2048
BOT2048_NTUPLE_WEIGHTS=tools/ntuple/run/w-l0.003.bin   # pinned explicitly (see below)
BOT2048_RECORD_ENGINE=1 ... (levers: docs/record-engine.md)
BOT2048_EVAL_MLP=0          # arm64 only: the MLP gather-split leaf is an x86 win,
                            # -8..15% on Apple M4 (experiment-log 2026-07-12)
BOT2048_SEARCH_WORKERS=4    # macOS: = performance cores (sysctl -n hw.perflevel0.physicalcpu)
```

- **Weights are pinned on purpose.** An arg-less launch probes `run/` for
  `w-l0.003.bin` then `champion-wall-snap-0316.bin` (`serve.go
  weightCandidates`) — the net would depend on which files exist. Both come
  from `scripts/fetch_weights.sh`.
- **`BOT2048_SEARCH_WORKERS`** — width of the parallel budget-search pool
  (budget-mode only; a fixed-depth `--depth 4` search never uses it). Code
  default is `NumCPU-2`. On the 8-core i7 `= NumCPU` (8) was best (the leaf is
  DRAM-latency bound, hyperthreads help). On the M4 (4P+6E) `NumCPU-2 = 8`
  would drag E-cores into the critical path and lengthen the tail: start at
  **4 = P-cores**, then sweep `{3,4,5,6,8,10}` with `perfbench -mode budget`
  and pick by mean AND p10 depth (audit B1). `GOMAXPROCS` also caps the
  prefetch fan-out (`serve.go`); try `GOMAXPROCS=6` if the prefetch join shows
  up in `search_ipc`.
- **Leaf-value cache is ON by default** (64 MB, bit-exact, never invalidates).
  Kill switch: `BOT2048_LEAF_CACHE=0`.

## Repeatable perf bench + bit-identity harness

```sh
# serial fixed-work (deterministic; prints a decision checksum = bit-identity gate
# WITHIN one GOARCH)
./ntuple perfbench -corpus <boards.txt> -depth 5 -cprob 1e-4 -reps 3 -cache=1
# workers sweep (parallel fixed-work throughput)
./ntuple perfbench -corpus <boards.txt> -depth 5 -cprob 1e-4 -workers 4 -cache=1
# endgame depth-per-budget (the number that matters for the live budget ladder)
./ntuple perfbench -corpus <boards.txt> -mode budget -budget 800 -workers 4
# regenerate the PGO profile ON THIS ARCH, then rebuild with -pgo=auto
./ntuple perfbench -corpus <boards.txt> -depth 5 -cprob 1e-4 -cache=1 -cpuprofile default.pgo
```

(`./ntuple.exe` on Windows.) `boards.txt` is not in git — the corpus lives on
the i7 (`tmp/`); copy it over or regenerate one from `data/games.db`.

Two builds (or cache on vs off) on the same architecture MUST print the
identical `checksum=...` on the serial (`-workers 1`) path. Measured numbers:
`docs/experiment-log.md`, entry `2026-07-12 перф-стек движка` (amd64) — the M4
needs its own entry once the sweep runs.

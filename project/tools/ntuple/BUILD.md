# Build & launch note — ntuple engine (perf stack)

## Production build (champion, optimized)

```sh
cd project/tools/ntuple
GOAMD64=v3 go build -pgo=auto -o ntuple.exe .
```

- `-pgo=auto` picks up `default.pgo` in this directory automatically (Go ≥1.21;
  we are on 1.24). The profile is an endgame CPU profile of the search leaf.
- `GOAMD64=v3` requires a Haswell-or-newer x86-64 CPU (POPCNT/BMI2/AVX2). Verified
  present on the target box (no SIGILL). On a pre-Haswell CPU, drop to the default
  (omit `GOAMD64`) — the binary is otherwise identical and still bit-exact.
- Both flags are **bit-exact**: Go's amd64 backend does not fuse FMA and PGO only
  changes inlining/layout, so search decisions are unchanged.

## Launch (live)

```sh
BOT2048_SEARCH_WORKERS=8 ./ntuple.exe        # serve (arg-less = serve)
```

- **`BOT2048_SEARCH_WORKERS=8`** — on this 8-core box, using all logical cores beats
  the conservative `NumCPU-2` default (the leaf is DRAM-latency bound, so hyperthreads
  help). Retune per box (`= NumCPU` was optimal here). The code default is unchanged.
- **Leaf-value cache is ON by default** (64 MB, bit-exact, never invalidates). Kill
  switch: `BOT2048_LEAF_CACHE=0`.

## Repeatable perf bench + bit-identity harness

```sh
# serial fixed-work (deterministic; prints a decision checksum = bit-identity gate)
./ntuple.exe perfbench -corpus <boards.txt> -depth 5 -cprob 1e-4 -reps 3 -cache=1
# workers sweep (parallel fixed-work throughput)
./ntuple.exe perfbench -corpus <boards.txt> -depth 5 -cprob 1e-4 -workers 8 -cache=1
# endgame depth-per-budget
./ntuple.exe perfbench -corpus <boards.txt> -mode budget -budget 800 -workers 8
# regenerate the PGO profile
./ntuple.exe perfbench -corpus <boards.txt> -depth 5 -cprob 1e-4 -cache=1 -cpuprofile default.pgo
```

Two builds (or cache on vs off) MUST print the identical `checksum=...` on the serial
(`-workers 1`) path. Measured numbers: `docs/experiment-log.md`, entry
`2026-07-12 перф-стек движка`.

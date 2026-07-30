---
name: 2048-simulation-discipline
description: "Operational rules for running monobank-2048 simulations and measuring the Go engine — CPU contention, sample sizes, what metric to judge by"
metadata: 
  node_type: memory
  type: project
  originSessionId: 9994db1d-76a1-45df-a78d-0d55139499dc
---

Rules learned the hard way while running monobank-2048 experiments (2026-07-11).

**CPU contention is the #1 self-inflicted wound.** A depth-6 game is a full CPU-second-heavy
job; the runner takes 13 workers. Launching several agents that each run games saturates the
14-core M4 Pro (load hit 73 on one occasion) and they starve each other — agents sat idle for
40+ minutes waiting for CPU. Rules:
- **At most ONE game-running job at a time.** Research agents that need games must be told to
  use `depth 4`, ≤30 games, or to reuse existing `results.jsonl` instead.
- **NEVER `pkill -f engine2048`** — one agent did this and killed every other agent's in-flight
  runs. Kill only your own PIDs.
- Long runs must be detached (`nohup ... & disown`), not run inside a Bash tool call — the
  600 s tool timeout SIGTERMs them mid-run (this destroyed one 4-arm campaign).
- **Stopping the AGENT does not stop its simulation.** `experiment_runner` launches a
  `multiprocessing` pool; when its master dies the pool workers **reparent to launchd (pid 1)**
  and keep spinning, each holding an `engine2048` subprocess — and a detached `run_rest.sh`-style
  supervisor will respawn whole arms. Killing single PIDs is a losing race (they respawn faster).
  The clean kill is **by process group**: `ps ax -o pgid,command | grep multiprocessing` to list
  the orphaned pools' pgids, then `kill -9 -<pgid>` per group — one shot per pool, and it leaves
  a different agent's Go binary (e.g. `ntupleT`, not multiprocessing) untouched. This is how ~89
  orphaned workers + 16 engines from a stopped risk-terms agent got cleaned in one pass.

**Under load, wall-clock measures the queue, not the code.** Benchmarks read 22 ms/op on a
quiet machine and 48 ms/op at load 55 — same binary. Measure **user-CPU** (`/usr/bin/time`),
**node counts**, or `go test -bench -count=5` with interleaved arms and take minimums. Game
LENGTHS are deterministic and load-independent, so quality metrics are safe under load; speed
metrics are not.

**Thermal:** `pmset -g therm` lies on Apple Silicon (reports nothing). Use
`sudo powermetrics --samplers thermal -n 1`. `Moderate` pressure was fixed by
`sudo pmset -c powermode 2` (High Power Mode, MacBook Pro 16" only) — benchmark went
31.7 → 21.6 ms/op at the same load.

**Sample sizes — n=12 is not data.** MDE at n=12 is ~5715 points; every lever in this project
is worth 50–200 moves. Two separate n=12 estimates overstated their effect by 4x when re-run
at n=100. Use n≥100 for the mean, **n≥400 to rank the tail** (84% of the uncertainty in the
tail z-score is σ, which converges slowly).

**Judge by the right metric.** The objective is `P(moves ≥ 2569)`, a tail. Selecting on the
mean is a systematic error — a profile with a *worse* mean and fatter σ can have a 5x better
tail. But the deeper truth found later: the tail is **truncated**, not thin (a deterministic
wall at ~2082 moves = 1895 + the 165–207-move life after the 4th reservoir). So the real
success criterion for any change is **"did any game exceed 2200 moves?"**, not μ and not σ.

See [[numbers-must-come-from-data]] for the epistemics side of this.

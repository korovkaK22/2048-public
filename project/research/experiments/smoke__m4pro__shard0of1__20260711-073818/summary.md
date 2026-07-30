# Experiment `smoke` — run `smoke__m4pro__shard0of1__20260711-073818`

- machine: **m4pro**  |  shard: **0/1**  |  seeds: **1000..1003** (4 games)
- depth: **6**  |  workers: **4**  |  GOMAXPROCS: **1**
- notes: Tiny mechanics check: 4 games, depth 6, baseline weights. Not for numbers — just to confirm the harness produces a run folder with summary.md + results. Use as the first-run example in the Mac runbook.

## Tail metrics (THE headline — we need ONE game >= 48k, not a good mean)

- **max score: 39,240**
- **P(score >= 48,000): 0.0%**  (>=45k: 0.0%, >=50k: 0.0%)
- reached 3x1024: **75.0%**  |  reached 4x1024: **25.0%**  |  reached 2x1024: 100.0%
- forced-2048 rate: **0.0%**

## Distribution

- mean: 32,110  |  median: 31,824.0  |  min: 25,552  |  stdev: 4,948.5
- max-tile histogram: 1024:4
- end reasons: game_over:4
- moves: mean 1,763.2  |  total 7,053

## Performance (compare machines: M4 vs Windows)

- wall time: **116.4 s**  (0.03 h)
- throughput: **124 games/hour**  |  **61 moves/s**
- engine cost: **54.908 ms/move** (mean, single-thread) 

## Weights

```json
{
  "BOT2048_FARM_EMPTY_W": 340,
  "BOT2048_FARM_MERGE_W": 140,
  "BOT2048_FARM_CORNER_W": 640,
  "BOT2048_FARM_EDGE_W": 0,
  "BOT2048_FARM_STAIR_W": 120,
  "BOT2048_FARM_SNAKE_W": 0.25,
  "BOT2048_FARM_SNAKE_RATIO": 0.85,
  "BOT2048_FARM_CORNER_RANK_W": 160
}
```

> Portable: this folder is self-contained. Zip it, move it, and merge
> with other shards of the same profile via `scripts/experiment_merge.py`.

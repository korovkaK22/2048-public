# Experiment `probe-d-cprob-1e5` — run `probe-d-cprob-1e5__m4pro__shard0of1__20260711-094902__243fbf83`

- machine: **m4pro**  |  shard: **0/1**  |  seeds: **1000..1012** (13 games)
- depth: **6**  |  workers: **13**  |  GOMAXPROCS: **1**
- notes: Timing probe only (13 seeds, 1 per worker): cost of cprob=1e-5.

## Tail metrics (THE headline — we need ONE game >= 48k, not a good mean)

- **max score: 41,164**
- **P(score >= 48,000): 0.0%**  (>=45k: 0.0%, >=50k: 0.0%)
- reached 3x1024: **100.0%**  |  reached 4x1024: **7.7%**  |  reached 2x1024: 100.0%
- forced-2048 rate: **7.7%**

## Distribution

- mean: 32,676.3  |  median: 32,456  |  min: 29,448  |  stdev: 2,866.7
- max-tile histogram: 1024:12  2048:1
- end reasons: game_over:12  tile_cap:1
- moves: mean 1,767.2  |  total 22,974

## Performance (compare machines: M4 vs Windows)

- wall time: **796.3 s**  (0.22 h)
- throughput: **59 games/hour**  |  **29 moves/s**
- engine cost: **422.645 ms/move** (mean, single-thread) 

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
  "BOT2048_FARM_CORNER_RANK_W": 160,
  "BOT2048_FARM_CONVEX_W": 80,
  "BOT2048_FARM_LATE_EMPTY_W": 120,
  "BOT2048_FARM_LATE_GRAD_MULT": 2.0,
  "BOT2048_FARM_LATE_CORNER_RANK_W": 320,
  "BOT2048_FARM_HOARD_TRIGGER": 99,
  "BOT2048_CPROB_THRESH": 1e-05
}
```

> Portable: this folder is self-contained. Zip it, move it, and merge
> with other shards of the same profile via `scripts/experiment_merge.py`.

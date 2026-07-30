# Experiment `benchmark` — run `benchmark__windows__shard0of1__20260711-100844__5a9b23d1`

- machine: **windows**  |  shard: **0/1**  |  seeds: **1000..1011** (12 games)
- depth: **6**  |  workers: **6**  |  GOMAXPROCS: **1**
- notes: SPEED benchmark — ФІКСОВАНИЙ конфіг для порівняння швидкості заліза (ця Windows-машина vs MacBook M4 Pro arm64). Ганяй ТОЙ САМИЙ конфіг на обох машинах; порівнюй perf.json. Ваги/depth/сіди/stop_tile ЗАФІКСОВАНІ й ІДЕНТИЧНІ на обох машинах — так порівнюється ЧИСТА пропускна здатність заліза, а не стратегія. Різниця лише у workers (беруться з профілю машини: windows=cpu-2, m4pro=13) — це і є вимірювана реальна пропускна здатність. Ваги = точні дефолти farm.go (як у baseline.json). Розмір (count=12) підібраний під ~5хв на ЦІЙ Windows-машині у ТИХОМУ стані (без інших farm-сим агентів): 1 farm-гра depth6 тут ~1700 ходів; калібрувальна проба під навантаженням дала ~180мс/хід single-thread і 6 ігор/533с на 6 воркерах, звідки з поправкою на тихий прогін (пришвидшення ~2-6x) count=12 лягає ~5хв і не опускається нижче 3хв у всьому діапазоні. count=12 = 2 гри/воркер на windows (усі 6 воркерів, рівний wall_s). ЯК ЗАПУСКАТИ: тут -> scripts/experiment.sh config/experiments/benchmark.json --machine windows ; на Mac -> scripts/experiment.sh config/experiments/benchmark.json --machine m4pro. ЩО ПОРІВНЮВАТИ в perf.json: moves_per_sec і games_per_hour (більше = швидша машина) — головні числа; wall_s теж (однаковий обсяг ігор на обох -> менший wall_s = швидша машина); ms_per_move_mean — вартість одного ходу движка. Тримай машину ТИХОЮ під час бенчмарку (жодних інших farm-сим/HEUR прогонів), інакше числа контамінуються. НЕ міняй weights/depth/seeds/stop_tile — інакше зламаєш порівнюваність між машинами.

## Tail metrics (THE headline — we need ONE game >= 48k, not a good mean)

- **max score: 37,544**
- **P(score >= 48,000): 0.0%**  (>=45k: 0.0%, >=50k: 0.0%)
- reached 3x1024: **58.3%**  |  reached 4x1024: **16.7%**  |  reached 2x1024: 83.3%
- forced-2048 rate: **0.0%**

## Distribution

- mean: 28,586.7  |  median: 30,188.0  |  min: 16,116  |  stdev: 7,011.4
- max-tile histogram: 1024:12
- end reasons: game_over:12
- moves: mean 1,571.7  |  total 18,860

## Performance (compare machines: M4 vs Windows)

- wall time: **288.9 s**  (0.08 h)
- throughput: **150 games/hour**  |  **65 moves/s**
- engine cost: **75.594 ms/move** (mean, single-thread) 

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
  "BOT2048_FARM_HOARD_TRIGGER": 99
}
```

> Portable: this folder is self-contained. Zip it, move it, and merge
> with other shards of the same profile via `scripts/experiment_merge.py`.

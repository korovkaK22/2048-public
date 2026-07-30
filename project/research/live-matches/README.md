# live-matches — телеметрія живих партій

Витяг з `project/data/games.db` (gitignored, `kind='live'`) — щоб дані про
живі прогони жили в git, а не тільки в локальній БД. Знято 2026-07-30.

## Файли
- `results.jsonl` — один JSON-об'єкт на партію (263 партії). Поля:
  - `id`, `experiment_id`, `strategy`, `run_id`, `git_rev`
  - `started_at`/`ended_at` (unix), `duration_s`, `end_reason`
  - `final_score`, `max_exponent`, `max_tile`
  - `num_moves` (з БД) / `num_moves_recorded` (фактично записаних ходів) / `num_spawns`
  - швидкість: `moves_per_s`, `eval_ms_mean`, `eval_ms_median`, `eval_ms_p95`, `eval_ms_max`
- `aggregates.json` — зведення overall + по кожному experiment (best/median score,
  total moves, середній eval_ms, середній moves/s).

## Зведення (overall)
- партій: **263** (219 з ходами), сумарно ходів: **318 364**
- найкращий score: **48 136** (experiment `ntuple-fast-resume`)
- медіанний score (по завершених): **29 870**
- середній eval движка: **~57 ms/хід**, темп: **~2.55 ходів/с**

## Що тут НЕМАЄ
Похідні board_before/board_after кожного ходу + спавни (повний postfactum-replay)
сюди не клали — завеликі для git. Повний дамп кожної партії (game + усі ходи +
спавни) лежить в архіві `live-matches-*.zip` (див. `live-matches/manifest.json`
всередині нього). Тут — тільки метрики: score, ходи, швидкість.

Регенерація: скрипт читає `data/games.db` і перезаписує ці файли (одноразовий,
у scratchpad).

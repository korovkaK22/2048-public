# docs/research — дослідницькі звіти

Тут лише **тексти** звітів (`.md`). Скрипти, сирі дані й результати експериментів, на які вони
посилаються, лишились у [`../../project/research/`](../../project/research/) — код і конфіги
звертаються до них за тими шляхами.

> **Шляхи в звітах.** Більшість звітів написано, коли вони лежали в `project/research/`, тому
> шляхи на кшталт `src/…`, `tools/…`, `scripts/…`, `tests/…`, `config/…`, `data/…` — **відносно
> `project/`**; `docs/…` — відносно кореня репозиторію. Посилання на сирі дані переписано на
> `project/research/…`.

Усі звіти — **зріз на свою дату**, не актуальна довідка. Що з них поїхало в код і з яким
результатом — [`../experiment-log.md`](../experiment-log.md) і
[`../training-and-runs-census.md`](../training-and-runs-census.md).

## Стратегія й рушій
- [`strategy-research.md`](strategy-research.md) — (2026-07-10) огляд алгоритмів для 2048: expectimax, MCTS, n-tuple/TD, з поправкою на ADB-свайп як вузьке місце.
- [`engine-adoption-research.md`](engine-adoption-research.md) — (2026-07-10) які готові опенсорс-рушії 2048 можна взяти замість/поруч зі своїм; ліцензії, адаптація під правила клону.
- [`path_to_50k.md`](path_to_50k.md) — (2026-07-11) чи можна побити 47k суперника: аналіз дір farm-евристики, проби depth 7, «джекпот»-профіль.
- [`strategy_candidates.md`](strategy_candidates.md) — (2026-07-11) список кандидат-стратегій для правого хвоста (3–4 × 1024) під A/B; ★1 = HEUR2 у `farm.go`.
- [`arm_20core_scaling.md`](arm_20core_scaling.md) — (2026-07-11) дизайн батч-симуляцій на 20-ядерному ARM: cross-compile Go, worker-pool, парні сіди.

## Латентність живого циклу
- [`live_latency_plan.md`](live_latency_plan.md) — (2026-07-11) план різати latency: settle + prefetch, вихідна картина 1.1–1.9 с/хід.
- [`swipe_throughput.md`](swipe_throughput.md) — (2026-07-11) скільки свайпів/с приймає гра: методи вводу × тривалість жесту.
- [`early_spawn_detection.md`](early_spawn_detection.md) — (2026-07-11) чи можна раніше побачити спавн і зрізати settle; P(4), анімація ~115 мс.
- [`on_device_processing.md`](on_device_processing.md) — (2026-07-11) наскільки прискорить он-девайс обробка кадру (MediaProjection, scrcpy control).
- [`latency-scout-device-pipeline.md`](latency-scout-device-pipeline.md) — ранжований огляд решти резерву латентності в device/UI-ланці (~330 мс/хід), нічого не впроваджено.

## Телеметрія, якість, тести
- [`latency_telemetry.md`](latency_telemetry.md) — (2026-07-11) дизайн per-move таймінгів; Tier A реалізовано (`moves.eval_details_json.timing`).
- [`telemetry_review.md`](telemetry_review.md) — (2026-07-11) аудит покриття телеметрії; топ-5 (M1–M3, N6) згодом реалізовано.
- [`code_review.md`](code_review.md) — (2026-07-11) read-only рев'ю Go-рушія, live-раннера, vision і харнесу експериментів.
- [`test_speed.md`](test_speed.md) — (2026-07-11) заміри й план прискорення pytest-сюїту (xdist тощо).

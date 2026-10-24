# Телеметрія латентності live-циклу — куди логувати, щоб ПОБАЧИТИ слабину

Дата: 2026-07-11. Статус: **дизайн-документ**; нижче — специфікація точок коду, схеми і звіту.

Що з цього поїхало в код: **Tier A (§2.2) реалізований у спрощеному вигляді** — п'ять фаз
`capture_ms / perceive_ms / detect_ms / swipe_ms / settle_ms` плюс `total_ms` у
`moves.eval_details_json.timing` (`runner/move_telemetry.py`, `_MovePhaseTimer`) і читач
`bot2048 db latency` (`storage/latency.py`). **Розширений набір ключів і лічильники кадрів/спроб
із §2.2, а також Tier B (§2.3) не реалізовані** — це так і лишилось дизайном.

## TL;DR

- Ціль: **повний per-move розклад часу** одного ходу live-циклу в БД, щоб з даних
  одразу було видно, яка фаза — бутылочне горло, без гадання.
- **Головна ідея вимірювання:** усі дельти — по **монотонному годиннику**, який
  вже інжектиться в раннер (`LiveRunner.__init__`, `clock=time.monotonic`, `self._clock`).
  Ніякого `time.time()` для тривалостей.
- **Куди писати без ризику для контракту:** `moves.eval_details_json` — це вже
  вільний JSON-бакет (`MovePlayed.eval_details: dict[str, Any]`, серіалізується
  `sqlite_recorder._dumps`). Кладемо туди неймспейс `"timing"`. **recording.py,
  schema.py, sqlite_recorder.py — не чіпаємо.** Це Tier A (рекомендовано).
- **Search вже частково виміряний:** `moves.eval_ms` (колонка) = повний decide-час
  зі стратегії (`perf_counter`), а `eval_details.engine_ms` = чистий пошук у Go, і
  `eval_details.prefetch_hit` — прапор «пошук був схований під анімацією». Тобто
  **IPC-overhead = eval_ms − engine_ms**, а критичний шлях пошуку відрізняється від
  спекулятивного (P19) по `prefetch_hit`. Це вже в даних — бракує лише читача.
- **Реальна діра** — фази `capture / detect_state / parse / swipe / settle-sleep`:
  сьогодні **жодна не пишеться**. Саме сюди йде час (див. `early_spawn_detection.md`),
  саме тут «слабина».
- Пріоритет-гіпотеза слабини (нижче §7): **(1) фіксований settle-sleep, (2) capture
  на фолбеку screencap-WiFi + к-сть стабілізаційних кадрів, (3) parse.** Search/IPC —
  **найменш імовірна** слабина завдяки prefetch.

---

## 1. Фази виміру одного ходу (з точками коду)

Порядок happy-path у `runner/live.py::LiveRunner._play_move` (+ `_observe_after_swipe`,
`_settled_board`, `_capture_game_frame`, `_parse_or_escalate`). Одиниці — **мілісекунди
(float)**, години — `self._clock()` (монотонний). Колонки-лічильники — **int**.

| # | Фаза | Що таймимо | Точка входу→виходу (файл::функція, вираз) | Сьогодні? |
|---|------|-----------|--------------------------------------------|-----------|
| 1 | **search (critical)** | чистий пошук у Go на критичному шляху | `ai/go_engine.py::choose` → `eval_details["engine_ms"]` (поле `<ms>` відповіді `search`) | **Є** (в details) |
| 1' | **search IPC-overhead** | stdio round-trip + `_speculate` prefetch-виклик | `eval_ms − engine_ms` (обчислюється у звіті) | Є (похідне) |
| 1'' | **search hidden (P19)** | пошук зроблено під анімацією попереднього ходу | `eval_details["prefetch_hit"]` == true ⇒ `engine_ms≈0` | **Є** (прапор) |
| 2 | **predict** | `engine.move(board, dir)` (передбачення дошки, чистий Python) | `live.py::_play_move`, `predicted = engine.move(state.board, decision.direction)` | Ні (мала) |
| 3 | **swipe (adb)** | тіло `device.swipe_direction(...)` | `live.py::_play_move`, `self.device.swipe_direction(...)` | **Ні** |
| 4 | **settle-sleep (fixed)** | навмисні `self._sleep(...)` після свайпу і між ретраями | `live.py::_play_move` `self._sleep(settle_delay_s + humanizer.next_delay())`; `_observe_after_swipe` `self._sleep(settle_delay_s)`; swipe-retry `self._sleep(swipe_retry_delay_s)` | **Ні** |
| 4' | **humanizer jitter** | джиттер-складова settle (людяність, не «робота») | значення `self.humanizer.next_delay()` (рядок 922) | **Ні** |
| 5 | **capture** | один кадр `device.screenshot()` (scrcpy-стрім або screencap) | `runner/escalation.py::probe_screen`, `image = device.screenshot()` (виклик з `live.py::_capture_game_frame`, `probe_screen(self.device, self.detect)`) | **Ні** |
| 6 | **detect_state** | класифікація екрана | `runner/escalation.py::probe_screen`, `detection = classify(image)` | **Ні** |
| 7 | **parse (perceive board)** | розпізнавання дошки з кадру | `live.py::_parse_or_escalate`, `self.read_board(frame)` | **Ні** |
| 8 | **verify/settle (total)** | увесь критичний «settle» = все всередині `_observe_after_swipe` | `live.py::_play_move`, обгортка навколо `outcome, parsed = self._observe_after_swipe(...)` | **Ні** |
| 9 | **move (total)** | повний хід від старту свайпу до коміту | `live.py::_play_move`, від першого `swipe_direction` до `_commit_move` | Ні |

Ключові множники (чому фази 5–7 — це суми, а не одиничні заміри):
`_observe_after_swipe` крутить `_settled_board` до `parse_retries` разів, а кожен
`_settled_board` чекає `settle_stable_frames` однакових парсів. **Кожен
стабілізаційний кадр = 1×capture + 1×detect_state + 1×parse.** Тому:
- `capture_ms/detect_ms/parse_ms` — **сума по всіх кадрах ходу**;
- поруч пишемо лічильники `capture_count`, `settle_frames_used`, `parse_retries_used`,
  `swipe_attempts` — щоб бачити, скільки разів фаза виконалась (стабілізація дорога
  саме кількістю кадрів, а не одним заміром).

### 1.1 Критичний шлях пошуку vs схований під анімацією (P19 prefetch)

`GoExpectimaxStrategy.choose` (go_engine.py) вже дає все для розрізнення:
- **prefetch-hit** (`_try_lookup` повернув попадання): `engine_ms = 0.0`,
  `prefetch_hit = True`, `eval_ms` = лише lookup-RTT + один `_speculate`-RTT.
  → **реальна вартість пошуку на критичному шляху ≈ IPC (десятки–сотні µs)**,
  сам expectimax відпрацював під settle-анімацією попереднього ходу.
- **miss / без prefetch**: `engine_ms > 0` **на критичному шляху**, `prefetch_hit`
  відсутній/False.

Звіт МУСИТЬ рахувати `search` окремо по `prefetch_hit` і показувати **hit-rate** —
інакше «середній search» змішає схований і критичний пошук і збреше. Нюанс: навіть на
hit `choose` робить fire-and-forget `_speculate` для НАСТУПНОЇ дошки (≈1 stdio-RTT) —
це підготовка наступного ходу, не пошук цього; входить в `ipc_ms`, тримати в голові.

---

## 2. Куди емітити

### 2.1 structlog (на кожному ході, з коробки, без БД-змін)

Один структурований лог-рядок **на хід**, зібраний у `_commit_move` перед/після запису:
`log.info("live.move_timing", game_id=…, ply=…, move_ms=…, verify_ms=…, capture_ms=…,
capture_count=…, detect_ms=…, parse_ms=…, swipe_ms=…, swipe_attempts=…,
settle_sleep_ms=…, engine_ms=…, eval_ms=…, prefetch_hit=…)`.
Дає живий потік для tail'у логів (обережно: **не грепати цей рядок high-freq фільтром** —
див. `docs/ops-playbook.md`). Також емітити цей самий словник у WARN-логах абортів
(`live.game_capture_aborted`, `_play_move` mismatch/rejected), бо аборт-хід у БД не
пише `moves`-рядок, а «повільний-але-впав» хід теж цінний для аналізу слабини.

### 2.2 Tier A — `moves.eval_details_json` (БЕЗ зміни контракту) ★ рекомендовано

`MovePlayed.eval_details` — це `dict[str, Any]`, який `sqlite_recorder` серіалізує в
`eval_details_json`. Раннер у `_commit_move` **мерджить** свій timing-словник у
`decision.details` під ключем `"timing"` перед створенням `MovePlayed`:

```jsonc
// eval_details_json  (приклад значення)
{
  "engine": "go", "eval": 1.23e6, "engine_ms": 0.0, "p4": 0.1,
  "farm": false, "prefetch_hit": true,
  "timing": {
    "move_ms": 812.4, "verify_ms": 690.1,
    "predict_ms": 0.03, "swipe_ms": 41.2, "swipe_attempts": 1,
    "settle_sleep_ms": 452.0, "poll_sleep_ms": 130.0, "humanizer_ms": 148.0,
    "capture_ms": 55.0, "capture_count": 3,
    "detect_ms": 9.0, "parse_ms": 44.0,
    "settle_frames_used": 2, "parse_retries_used": 1,
    "capture_kind": "scrcpy"      // "scrcpy" | "screencap" — джерело кадру
  }
}
```

Точний неймспейс `timing.*` (ключ : тип, одиниці):

| ключ | тип | одиниці | зміст |
|------|-----|---------|-------|
| `move_ms` | float | ms | повний хід (свайп→коміт), фаза 9 |
| `verify_ms` | float | ms | весь `_observe_after_swipe`, фаза 8 (headline «settle») |
| `predict_ms` | float | ms | `engine.move` предикт, фаза 2 |
| `swipe_ms` | float | ms | сума `swipe_direction`, фаза 3 |
| `swipe_attempts` | int | — | к-сть свайп-спроб (1 = з першого разу) |
| `settle_sleep_ms` | float | ms | сума фіксованих settle-sleep (без polls), фаза 4 |
| `poll_sleep_ms` | float | ms | сума стабілізаційних `settle_poll_s`-пауз |
| `humanizer_ms` | float | ms | джиттер-складова (фаза 4') — окремо від реального settle |
| `capture_ms` | float | ms | сума `device.screenshot()`, фаза 5 |
| `capture_count` | int | — | к-сть захоплених кадрів за хід |
| `detect_ms` | float | ms | сума `classify()` (detect_state), фаза 6 |
| `parse_ms` | float | ms | сума `read_board()`, фаза 7 |
| `settle_frames_used` | int | — | скільки стабіл. кадрів фактично взято |
| `parse_retries_used` | int | — | скільки ретраїв верифікації використано |
| `capture_kind` | str | — | джерело кадру: `scrcpy`/`screencap` (щоб різати p95 по транспорту) |

`engine_ms`, `eval_ms`, `prefetch_hit` **НЕ дублюємо** в `timing` — вони вже є
(колонка `eval_ms` + `eval_details` зі стратегії). Звіт бере їх звідти.

Плюси Tier A: нуль ризику для заморожених контрактів, працює без змін схеми —
через `json_extract`. Мінус: JSON-екстракт не індексується; для сотень тисяч рядків
все одно ок (одна гра ~ сотні ходів).

### 2.3. Tier B — first-class additive колонки в `moves` (НЕ реалізовано)

Якщо знадобиться індексований/швидкий агрегат по мільйонах ходів — винести найгарячіші
фази в **нові NULLABLE колонки** `moves`. Це зачіпає **заморожений контракт**, тому
робиться лише як окремо узгоджена зміна (правки в трьох файлах + `MovePlayed`):

Пропоновані колонки (усі `REAL`/`INTEGER`, `NULL` для старих рядків і для sim-ігор):

```sql
-- ALTER TABLE moves ADD COLUMN ...  (порядок — як у SCHEMA_SQL)
verify_ms        REAL,     -- фаза 8: весь settle/observe
capture_ms       REAL,     -- фаза 5: сума screenshot
detect_ms        REAL,     -- фаза 6: сума detect_state
parse_ms         REAL,     -- фаза 7: сума read_board
swipe_ms         REAL,     -- фаза 3
settle_sleep_ms  REAL,     -- фаза 4 (fixed sleeps, без polls/humanizer)
move_ms          REAL,     -- фаза 9
capture_count    INTEGER,  -- лічильник кадрів
swipe_attempts   INTEGER,  -- лічильник свайпів
capture_kind     TEXT      -- 'scrcpy' | 'screencap'
```

Що доведеться змінити (це заморожені/контрактні файли):
- `src/bot2048/recording.py` — додати відповідні поля у `@dataclass MovePlayed`
  (усі з дефолтами, щоб не ламати позиційних викликів). **FROZEN-контракт.**
- `src/bot2048/storage/schema.py` — `SCHEMA_SQL` `CREATE TABLE moves` + міграція
  `ALTER TABLE` для наявних БД. **Зміна схеми БД.**
- `src/bot2048/storage/sqlite_recorder.py` — `_INSERT_MOVE`, тип `_move_rows`,
  та кортеж у `case MovePlayed()` (порядок значень!). **Контрактний файл.**

**Рекомендація:** старт на **Tier A** (нуль контрактного ризику). Перейти на Tier B
лише якщо звіт по JSON почне гальмувати або знадобиться індекс по фазі. Дизайн полів
однаковий, тож Tier A→B — це механічне перенесення ключів `timing.*` у колонки.

---

## 3. Як інструментувати без правок у shared-коді

Проблема: `capture` (фаза 5) і `detect_state` (фаза 6) заховані всередині
`escalation.probe_screen` — а це **спільна** функція (нею користується і супервізор).
Міняти її сигнатуру заради timing — погано.

**Рішення — timing-shim у раннері (мінімально інвазивно, нуль змін у shared-коді):**
1. Легкий мутабельний акумулятор (slotted dataclass із float/int-полями), поле раннера
   `self._move_timing`. **Reset на початку `_play_move`.**
2. У `LiveRunner.__init__` обгорнути `self.device.screenshot` і `self.detect` тонкими
   шимами, що додають монотонну дельту в поточний `self._move_timing` (capture/detect).
   `probe_screen`/`escalation.py` лишаються недоторкані — вони кличуть уже обгорнуті
   об'єкти.
3. `parse` (`_parse_or_escalate`→`read_board`), `swipe`, `predict` раннер кличе САМ —
   таймити інлайн (`t=self._clock(); …; acc.parse_ms += (self._clock()-t)*1000`).
4. Навмисні паузи (`self._sleep`) додавати в `settle_sleep_ms`/`poll_sleep_ms` за
   аргументом (значення відоме), `humanizer_ms` = значення `humanizer.next_delay()`.
5. search — не таймити тут: узяти `decision.eval_ms` (колонка) і `decision.details`
   (`engine_ms`, `prefetch_hit`).
6. У `_commit_move` «запечатати» акумулятор у словник і покласти під
   `decision.details["timing"]` (Tier A) або у нові поля `MovePlayed` (Tier B).

Такий shim не змінює жодного контракту й дозволяє тестам ганяти таймінги на фейках
(годинник уже інжектиться — у тестах монотонний фейк дає детерміновані дельти).

---

## 4. Звіт-згортка: p50/p90/p95/max по фазах + частка критичного шляху

Мета — щоб слабина була **вгорі таблиці**. Формат виводу (rich, CLI `bot2048 db latency
[--experiment X] [--game G]`), відсортовано за `%move` спадаюче:

```
Latency breakdown — experiment=live  games=7  moves=1832  prefetch_hit=61%  ~1.4 moves/s
phase           n     p50     p90     p95     max    mean   %move
settle_sleep  1832   452.0   601.0   604.0   612.0   470.1   57.9%   ← слабина
capture       1832    55.0   180.0   640.0   910.0    98.4   12.1%
parse         1832    44.0    70.0    95.0   150.0    48.7    6.0%
verify(total) 1832   690.0   980.0  1180.0  2040.0   742.3   (= 5+6+7+polls)
swipe         1832    41.0    58.0    70.0   120.0    43.2    5.3%
detect        1832     9.0    14.0    19.0    40.0    10.1    1.2%
search(miss)   715    82.0   140.0   180.0   310.0    95.0    critical
search(hit)   1117     0.3     0.6     0.9     3.0     0.4    hidden (P19)
```

Заголовок дає одразу: hit-rate prefetch, темп ходів/с (реальна стеля гри ~4–5/с — див.
`early_spawn_detection.md`), і n. Рядок з найбільшим `%move` — і є слабина.

### 4.1 SQL (Tier A, чистий SQLite, nearest-rank через window-функції)

Перцентилі без розширень: unpivot фаз у `(phase, ms)`, ранжування вікном, вибірка за
nearest-rank індексом. Копіювати-вставити (підстав `:exp` або прибери WHERE):

```sql
WITH mv AS (
  SELECT m.eval_ms, m.eval_details_json AS j
  FROM moves m JOIN games g ON g.id = m.game_id
  WHERE g.experiment_id = :exp          -- або прибрати рядок для всіх
),
samples AS (
  SELECT 'search_ms'       AS phase, eval_ms                                    AS ms FROM mv
  UNION ALL SELECT 'engine_ms',      CAST(json_extract(j,'$.engine_ms')     AS REAL) FROM mv
  UNION ALL SELECT 'move_ms',        CAST(json_extract(j,'$.timing.move_ms') AS REAL) FROM mv
  UNION ALL SELECT 'verify_ms',      CAST(json_extract(j,'$.timing.verify_ms') AS REAL) FROM mv
  UNION ALL SELECT 'capture_ms',     CAST(json_extract(j,'$.timing.capture_ms') AS REAL) FROM mv
  UNION ALL SELECT 'detect_ms',      CAST(json_extract(j,'$.timing.detect_ms') AS REAL) FROM mv
  UNION ALL SELECT 'parse_ms',       CAST(json_extract(j,'$.timing.parse_ms') AS REAL) FROM mv
  UNION ALL SELECT 'swipe_ms',       CAST(json_extract(j,'$.timing.swipe_ms') AS REAL) FROM mv
  UNION ALL SELECT 'settle_sleep_ms',CAST(json_extract(j,'$.timing.settle_sleep_ms') AS REAL) FROM mv
  UNION ALL SELECT 'poll_sleep_ms',  CAST(json_extract(j,'$.timing.poll_sleep_ms') AS REAL) FROM mv
),
ranked AS (
  SELECT phase, ms,
         row_number() OVER (PARTITION BY phase ORDER BY ms) AS rn,
         count(*)     OVER (PARTITION BY phase)             AS n
  FROM samples WHERE ms IS NOT NULL
),
-- nearest-rank: індекс перцентиля p = CAST(p*(n-1) AS INT)+1  (1-based, без CEIL)
agg AS (
  SELECT phase, n,
    MAX(CASE WHEN rn = CAST(0.50*(n-1) AS INT)+1 THEN ms END) AS p50,
    MAX(CASE WHEN rn = CAST(0.90*(n-1) AS INT)+1 THEN ms END) AS p90,
    MAX(CASE WHEN rn = CAST(0.95*(n-1) AS INT)+1 THEN ms END) AS p95,
    MAX(ms)                                                   AS max_ms,
    ROUND(AVG(ms), 1)                                         AS mean_ms,
    ROUND(SUM(ms), 1)                                         AS sum_ms
  FROM ranked GROUP BY phase
),
tot AS (SELECT sum_ms AS move_sum FROM agg WHERE phase = 'move_ms')
SELECT a.phase, a.n, a.p50, a.p90, a.p95, a.max_ms, a.mean_ms,
       ROUND(100.0 * a.sum_ms / NULLIF((SELECT move_sum FROM tot), 0), 1) AS pct_move
FROM agg a
ORDER BY a.sum_ms DESC;
```

Prefetch-розріз search (окремий маленький запит для заголовка + рядків search):

```sql
SELECT
  SUM(CASE WHEN json_extract(eval_details_json,'$.prefetch_hit')=1 THEN 1 ELSE 0 END) AS hits,
  COUNT(*)                                                                             AS n,
  ROUND(100.0*SUM(CASE WHEN json_extract(eval_details_json,'$.prefetch_hit')=1
                       THEN 1 ELSE 0 END)/COUNT(*), 1)                                 AS hit_pct,
  ROUND(AVG(CASE WHEN json_extract(eval_details_json,'$.prefetch_hit')=1
                 THEN CAST(json_extract(eval_details_json,'$.engine_ms') AS REAL) END),3) AS engine_ms_hit,
  ROUND(AVG(CASE WHEN COALESCE(json_extract(eval_details_json,'$.prefetch_hit'),0)=0
                 THEN CAST(json_extract(eval_details_json,'$.engine_ms') AS REAL) END),1) AS engine_ms_miss
FROM moves m JOIN games g ON g.id=m.game_id
WHERE g.experiment_id = :exp AND m.ply >= 0;
```

Нюанс nearest-rank у чистому SQLite достатньо для «побачити слабину» (±1 семпл на
хвостах). Для точних перцентилів рекомендований **тонкий Python-редюсер** (нижче) —
його ж варто зробити тілом CLI `bot2048 db latency`.

### 4.2 Python-редюсер (точні перцентилі; рекомендовано для CLI)

~15 рядків: витягнути стовпці `eval_ms` + `json.loads(eval_details_json)["timing"]`,
зібрати `numpy`-масиви по фазах, `np.percentile(arr, [50,90,95])`, `arr.max()`,
`arr.sum()/move_sum`, розрізати search по `prefetch_hit`, вивести rich-таблицею,
відсортованою за `%move`. Точні перцентилі + читабельний код; БД-запит тривіальний
(`SELECT eval_ms, eval_details_json FROM moves JOIN games ...`).

---

## 5. Оверхед самої телеметрії (щоб вимір не спотворював результат)

- **Монотонний годинник:** усі дельти — `self._clock()` (вже інжектований
  `time.monotonic`), для search — `perf_counter` у стратегії. Ніколи `time.time()`
  для тривалостей (він не монотонний; стрибок NTP зіпсує захват).
- **Вартість читання годинника** ~0.1–0.3 µs; на хід ~10–15 читань ⇒ **одиниці µs**
  проти фаз у 10–650 ms — тобто **<0.01 %**. Невимірно.
- **Без alloc у гарячому циклі:** акумулятор — slotted dataclass із фіксованими
  float/int-полями (мутуємо на місці, `+=`), **жодного dict/f-string на суб-фазі**.
  Словник `timing` збирається **один раз** у `_commit_move`.
- **Один лог-рядок на хід** (`live.move_timing`), не на суб-фазу — structlog I/O
  тримається поза capture/parse-циклом. Ніякого логування всередині
  `_settled_board`/`probe_screen`.
- **Шими на `screenshot`/`detect`** додають рівно одну пару читань годинника на
  виклик — та сама µs-вартість; не змінюють поведінки (просто накопичують).
- JSON-серіалізація `timing` — це +~150 байт у вже наявному `eval_details_json`
  запису, у тому ж `executemany`-батчі; додаткового I/O-раунду немає.

---

## 6. Що вже логується сьогодні vs дельта

**Є (у БД):**
- `moves.eval_ms` — повний decide-час (стратегія, `perf_counter`).
- `moves.eval_details_json.engine_ms` — чистий Go-пошук.
- `moves.eval_details_json.prefetch_hit` — прапор схованого пошуку (P19).
- `moves.search_depth` — досягнута глибина.
- `games.duration_s` — тільки повна тривалість гри (не per-move, від in-game таймера).

**Є (лише structlog, не в БД):** `live.game_started/ended`, `live.swipe_unregistered`,
`live.settle_timeout`, `live.game_capture_aborted` — події, без розкладу часу фаз.

**Дельта (бракує — саме це проектуємо):**
- ❌ `capture_ms` / `capture_count` / `capture_kind` — час і к-сть захоплень кадру.
- ❌ `detect_ms` — час detect_state.
- ❌ `parse_ms` — час розпізнавання дошки.
- ❌ `swipe_ms` / `swipe_attempts` — час і повтори adb-свайпу.
- ❌ `settle_sleep_ms` / `poll_sleep_ms` / `humanizer_ms` — навмисні паузи (найбільша
  підозра на слабину, зараз повністю невидимі).
- ❌ `verify_ms` / `move_ms` — headline-тотали (settle і повний хід).
- ❌ `settle_frames_used` / `parse_retries_used` — скільки стабіл. кадрів реально взято.
- ❌ IPC-overhead як окрема величина — похідне (`eval_ms − engine_ms`), бракує лише у звіті.

Тобто: **search-бік майже покритий, device/vision-бік — сліпа зона.** Весь дизайн вище
закриває саме device/vision-фази + тотали + лічильники.

---

## 7. Пріоритет: які 2–3 фази найімовірніше слабина (гіпотеза)

З `early_spawn_detection.md` (анімація ходу ≈115 мс, а happy-path settle
≈2.0 с screencap / ≈0.85 с стрім) і структури циклу:

1. **`settle_sleep_ms` (фаза 4) — найімовірніша слабина №1.** Фіксований
   `settle_delay_s=0.45 с` (×4 від 115-мс анімації) застосовується після свайпу і
   між ретраями; на happy-path це найбільший одиничний внесок. Телеметрія покаже його
   як топ-рядок `%move`. Це те, що ріже предикт-гейт із early-spawn-ресерчу.
2. **`capture_ms` + `capture_count` (фаза 5) — №2, залежить від транспорту.** На
   scrcpy-стрімі — десятки мс; на фолбеку **screencap-WiFi ≈0.65 с/кадр**, а кадрів за
   хід кілька (стабілізація ×2 + ретраї). `capture_kind` у звіті одразу покаже, коли
   стрім впав на screencap і p95 вистрелив — це «прихована» слабина, яку без розрізу за
   транспортом не видно.
3. **`parse_ms` (фаза 7) — №3.** Розпізнавання всієї дошки на кожному стабіл. кадрі,
   ×(settle_frames+retries). Не домінує, але множиться кількістю кадрів; варто бачити.

**Найменш імовірна слабина — search/IPC.** Завдяки P19 prefetch більшість ходів мають
`prefetch_hit=True` ⇒ `engine_ms≈0` (пошук схований під анімацією), а критичний шлях —
лише IPC (десятки–сотні µs). Навіть на miss `engine_ms` — десятки–сотні мс проти
сотень мс–секунд у settle/capture. Тому search окремо розрізаємо по `prefetch_hit`, але
не очікуємо його вгорі таблиці.

Підсумок гіпотези: **settle-sleep > capture(screencap) > parse ≫ search/IPC.**
Телеметрія має це підтвердити або спростувати з першого ж live-прогону — власне заради
цього («побачити слабину») вона і проектується.

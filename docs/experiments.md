# Experiment harness — farm-heuristic tuning для 50k / 4×1024

> Єдина, проста, відтворювана структура експериментів для тюнінгу farm-евристики
> (мета: побити лідера 47k → цільові 50k / чотири 1024). Один конфіг, одна
> команда, самодостатня підпапка результатів, яку можна зіпнути й перенести.
>
> Контекст стратегії — `docs/research/path_to_50k.md`. Масштабування на
> ARM — `docs/research/arm_20core_scaling.md`. Farm-ваги живуть у
> `project/tools/engine2048/farm.go`.

## 0. TL;DR (один прогін)

```bash
cd project
scripts/experiment.sh config/experiments/baseline.json --machine windows
# → research/experiments/baseline__windows__shard0of1__<ts>/summary.md
```

Все. Результат — самодостатня підпапка з `summary.md`, `results.jsonl`,
`aggregates.json`, `perf.json`, `config.json`.

---

## 1. Що робить харнес

Пайплайн (нічого нового не винаходить — той самий симулятор, що й CLI):

1. читає JSON-конфіг (ваги + сіди + depth);
2. експортує farm-ваги як env-змінні `BOT2048_FARM_*` (farm.go читає їх раз, на
   старті движка) + `GOMAXPROCS`;
3. ганяє N farm-ігор (`GoExpectimaxStrategy(farm=True)` через
   `bot2048.sim.runner.play_game`), workers-паралельно (один persistent
   Go-движок на воркер, `GOMAXPROCS=1`), з табу-правилом **2048 = кінець гри**
   (`stop_tile: 2048`);
4. пише самодостатню підпапку `research/experiments/<run-id>/`.

Файли (`project/scripts/`):

| файл | що |
|---|---|
| `experiment.sh` | тонка обгортка: обирає venv-інтерпретатор і форвардить аргументи |
| `experiment_runner.py` | ядро: конфіг → шардинг → паралельний прогін → агрегати → підпапка |
| `experiment_merge.py` | злиття кількох підпапок (той самий профіль, різні шарди) в один сумарний звіт |

---

## 2. Формат конфігу (`config/experiments/*.json`)

JSON (без нових залежностей; коментарі — у полі `notes`). Кожне поле:

| поле | тип | опис |
|---|---|---|
| `label` | str | коротке ім'я профілю; входить у run-id і в БД |
| `notes` | str | людський опис профілю (для себе й для того, хто читає результати) |
| `depth` | int | глибина expectimax. **Дефолт 6** (робоча стеля поточної евристики; d7 без структурних змін шкодить — див. path_to_50k §4) |
| `adaptive` | bool | адаптивна глибина движка. Для чистого порівняння ваг тримати `false` |
| `stop_tile` | int | табу-плитка: тільки-но зібрано таку плитку — гра закінчується. **2048** (правило monobank) |
| `max_moves` | int | запобіжник від нескінченної гри (farm-гра ~1500–3000 ходів; 6000 з запасом) |
| `seeds.base` | int | перший сід |
| `seeds.count` | int | скільки сідів (гра `i` = `base+i`) |
| `weights` | object | усі 8 farm-env-ваг (див. нижче). Значення → env для движка |
| `workers` | int? | (опц.) дефолтна к-сть воркерів; зазвичай задається профілем машини / `--workers` |
| `gomaxprocs` | int? | (опц.) дефолт 1 (батч-режим, без 4-way внутрішньої паралельності) |
| `engine_bin` | str? | (опц.) шлях до бінаря движка; `null` = автопошук (`--engine-bin` перекриває) |
| `p4` | float? | (опц.) P(спавн=4); `null` = взяти з `SpawnModel` (0.10 класика) |

**Farm-ваги** (`weights`) — усі 8 env-змінних, що їх читає `farm.go`
(`initFarmTables`); значення в S-одиницях, тримати `< 9216`, щоб не зламати
інваріант `farmEval ≥ sSum` (інакше движок почне будувати 2048):

| env-ключ | farm.go default | що робить |
|---|---|---|
| `BOT2048_FARM_EMPTY_W` | 340 | цінність однієї вільної клітини (виживання) |
| `BOT2048_FARM_MERGE_W` | 140 | готова пара під merge (rank 1..9) |
| `BOT2048_FARM_CORNER_W` | 640 | плоский бонус за big-tile (rank≥8) у куті |
| `BOT2048_FARM_EDGE_W` | 0 | big-tile на некутовому краю (склад під 2-й/3-й 1024) |
| `BOT2048_FARM_STAIR_W` | 120 | сусідство big-tiles з rank-різницею 1 (щільна стіна 1024\|512) |
| `BOT2048_FARM_SNAKE_W` | 0.25 | вага snake-градієнта (монотонність від кута — головний структурний член) |
| `BOT2048_FARM_SNAKE_RATIO` | 0.85 | покроковий спад snake-градієнта, (0,1] |
| `BOT2048_FARM_CORNER_RANK_W` | 160 | rank-масштабований кутовий бонус (1024-у-куті >> 256-у-куті) |

Приклади-конфіги:

- **`baseline.json`** — точні дефолти з farm.go. Опорна точка.
- **`jackpot.json`** — «джекпот»-профіль на хвіст (path_to_50k §5.2): нижче
  виживання, сильніший градієнт + edge-склади + жорсткі rank-кути → більше
  форс-2048 і нижчий min, але товщий правий хвіст. Метрика — **max і P(≥48k)**.
- **`snake-heavy.json`** — структура-first: важкий snake-градієнт + rank-кути +
  сходинки, помірне виживання.
- **`smoke.json`** — 4 гри, для перевірки механіки (не для чисел).

> **ПАРНІ СІДИ (важливо):** щоб порівнювати профілі коректно, тримай
> `seeds.base` і `seeds.count` **однаковими** в усіх конфігах, які порівнюєш.
> Тоді всі профілі грають той самий набір спавнів → парний тест, менша дисперсія
> різниці (arm_20core_scaling §4.3).

---

## 3. Профілі машин і одна команда

`--machine` виставляє дефолти воркерів/GOMAXPROCS і тегує run-id + perf.json:

| `--machine` | workers (дефолт) | GOMAXPROCS | нотатка |
|---|---|---|---|
| `m4pro` | 13 | 1 | MacBook M4 Pro (14 ядер, лишаємо 1 під OS), arm64 |
| `windows` | `cpu-2` | 1 | поточна Windows-машина |
| будь-що інше | `os.cpu_count()` | 1 | довільна мітка (напр. `--machine linux-box`) |

`--workers N` завжди перекриває дефолт профілю.

```bash
# M4 Pro, повний профіль на 200 сідів:
scripts/experiment.sh config/experiments/jackpot.json --machine m4pro

# те саме, але явно 10 воркерів (напр. паралельно з іншою роботою):
scripts/experiment.sh config/experiments/jackpot.json --machine m4pro --workers 10
```

---

## 4. Розділити ОДИН прогін на ДВІ машини + злити

Той самий конфіг, disjoint-сіди через `--shard IDX/COUNT`. Машина A бере
першу половину діапазону сідів, машина B — другу; разом покривають повний
`base..base+count` рівно один раз. run-id включає мітку машини+шарда, тож дві
підпапки не колізяться.

```bash
# --- на MacBook M4 Pro (перша половина сідів 1000..1099): ---
cd project
scripts/experiment.sh config/experiments/baseline.json --machine m4pro --shard 0/2

# --- на Windows-машині (друга половина 1100..1199): ---
cd project
scripts/experiment.sh config/experiments/baseline.json --machine windows --shard 1/2
```

Обидва пишуть у свій `research/experiments/<run-id>/`. Переносиш windows-підпапку
на Mac (або обидві в одне місце — див. §6) і зливаєш:

```bash
scripts/experiment_merge.py \
    research/experiments/baseline__m4pro__shard0of2__<ts> \
    research/experiments/baseline__windows__shard1of2__<ts>
# → research/experiments/baseline__merged__<ts>/summary.md  (спільні агрегати)
```

`experiment_merge.py`:
- **відмовляється** зливати різні вагові профілі (звіряє `weights`);
- де-дублікує за сідом (disjoint-шарди не мають перетинатись; якщо є — попереджає);
- рахує ТІ САМІ агрегати, що й одиночний прогін → merged-summary прямо
  порівнюваний з одиночним.

> Порада: тримай `shard_count` = к-сть машин, а `seeds.count` кратним їй, щоб
> половини були рівні. Приклад: 2 машини × `count 200` → по 100 ігор кожна.

---

## 5. Структура результату (самодостатня підпапка)

```
research/experiments/<label>__<machine>__shard<i>of<n>__<timestamp>/
├── config.json       # verbatim-знімок конфігу, що дав цей прогін
├── results.jsonl     # один JSON-об'єкт на гру (сирі per-game дані)
├── aggregates.json   # машиночитні агрегати (mean/max/tail/reservoirs)
├── perf.json         # таймінги (wall, games/hour, moves/s, ms/move, машина)
└── summary.md        # людський звіт; заголовок = ХВІСТ (max, P≥48k)
```

**`results.jsonl`** (одна гра / рядок):
```json
{"seed":1000,"score":39240,"moves":2108,"max_exponent":10,"max_tile":1024,
 "peak_1024":4,"final_1024":4,"end_reason":"game_over","forced_2048":false,
 "duration_s":254.2,"engine_ms_mean":120.4,"final_board":"a5a17a82464512a3"}
```
- `peak_1024` — **пік** одночасних 1024 за гру (головний хвостовий сигнал:
  «дійшов до 3×/4×1024»; 1024 не ростуть, форс-merge їх зменшує, тож саме пік,
  а не фінал, — правильна метрика);
- `forced_2048` — гра закінчилась зібраним 2048 (`end_reason == "tile_cap"`);
- `final_board` — 16 hex-цифр (експонента на клітину) для довідки/replay.

**`aggregates.json`** — headline-метрика ХВОСТА: `score_max`,
`p_score_ge` (`45000`/`48000`/`50000`), `pct_reach_3x1024`, `pct_reach_4x1024`,
`forced_2048_rate`, `max_tile_hist`; плюс `score_mean/median/min/stdev`, ходи.

**`perf.json`** — для порівняння M4 vs Windows: `wall_s`, `games_per_hour`,
`moves_per_sec`, `ms_per_move_mean` (з телеметрії движка), `workers`,
`gomaxprocs`, `machine`, `arch`, `games`, діапазон сідів.

> **Повна per-ply телеметрія (опційно):** прапорець `--record-db` додатково
> пише SQLite `data/experiments/<run-id>/wNN.db` (по одній на воркер) для
> повного replay (`experiments→games→moves+spawns`). `data/` — gitignored і
> host-local, тож у портативну підпапку НЕ входить. За замовчуванням вимкнено
> (сотні ігор × per-ply — це багато; для хвостового аналізу вистачає JSONL).

---

## 6. Портативність — як переносити й читати результати

Підпапка самодостатня: **зіпнув → переніс → розпакував**.

```bash
# на машині, де відбувся прогін:
cd project/research/experiments
zip -r baseline__windows__shard1of2__<ts>.zip baseline__windows__shard1of2__<ts>/
# → перекинути файл на машину, де робиш аналіз, і розпакувати в
#   project/research/experiments/.
```

`research/experiments/**` (крім `raw/` і `*.db`) трекається git-ом — тож
альтернатива zip-у: запушити гілку з прогоном і змержити її там, де аналізуєш;
підпапки приїдуть разом із кодом. `perf.json`/`aggregates.json`/`results.jsonl` —
усе, що потрібно для аналізу.

**Що читати:** `aggregates.json` — готові агрегати; `results.jsonl` — сирі
per-game дані для власного аналізу/графіків; `perf.json` — щоб порівняти
швидкість машин. Для пулу шардів з різних машин (той самий профіль) —
`experiment_merge.py <dirA> <dirB> ...` дає спільні агрегати.

---

## 7. Runbook для другої машини (POSIX / arm64) — крок за кроком

> Як підняти харнес на швидкій багатоядерній машині-помічниці (напр. MacBook
> M4 Pro, arm64) і ганяти там ті самі профілі. Команди — по порядку; все
> нативно на хості (ADB/Docker для цього не потрібні; Docker — лише
> альтернатива для збірки движка).

### 7.1 Клон і Python-середовище

```bash
git clone <origin репозиторію> monobank-2048
cd monobank-2048/project

python3.12 -m venv .venv            # потрібен Python 3.12
./.venv/bin/python -m pip install -U pip
./.venv/bin/python -m pip install -e ".[dev]"
```

(На Mac venv-бінарі — `.venv/bin/…`, не `.venv/Scripts/…`. `experiment.sh` це
визначає сам.)

### 7.2 Зібрати Go-движок під arm64 (нативно)

```bash
# потрібен Go >= 1.24 (brew install go)
cd tools/engine2048
CGO_ENABLED=0 go build -o engine2048 .        # чистий stdlib, без cgo — збереться чисто
printf 'ping\n' | ./engine2048                 # очікуємо: pong
cd ../..
```

Резолвер движка сам знайде `tools/engine2048/engine2048` на POSIX
(`--engine-bin` не потрібен). Farm — рантайм-прапор того самого бінаря.

**Альтернатива — через Docker** (якщо Go на хості не хочеш ставити):
```bash
# з project/: збере arm64-движок у stage 1 і випече образ (нативно, без QEMU)
docker build -t bot2048 .
# витягнути бінар з образу на хост, щоб ганяти харнес нативно:
id=$(docker create bot2048); docker cp "$id":/app/tools/engine2048/engine2048 \
    tools/engine2048/engine2048; docker rm "$id"
```

### 7.3 Санітарний прогін (механіка)

```bash
scripts/experiment.sh config/experiments/smoke.json --machine m4pro
ls research/experiments/                         # має з'явитись smoke__m4pro__shard0of1__<ts>/
cat research/experiments/smoke__m4pro__*/summary.md
```

Якщо бачиш `summary.md` з mean/max/tail — харнес живий.

### 7.4 Реальний прогін профілю

```bash
# повний профіль на 200 парних сідів, 13 воркерів:
scripts/experiment.sh config/experiments/jackpot.json --machine m4pro
# результат: research/experiments/jackpot__m4pro__shard0of1__<ts>/
```

Порівняти три профілі (парні сіди — усі три конфіги мають однакові base/count):
```bash
for p in baseline jackpot snake-heavy; do
    scripts/experiment.sh config/experiments/$p.json --machine m4pro
done
```

### 7.5 Де результати і як їх перенести

Усі підпапки — у `project/research/experiments/`. Перенести:
- **git**: закомітити й запушити гілку з прогоном, потім змержити її там, де
  аналізуєш (підпапки трекаються, крім `*.db`/`raw/`); АБО
- **zip**: `cd project/research/experiments && zip -r <run>.zip <run>/` і
  перекинути файл.

### 7.6 (Опц.) Розділити один прогін на дві машини

Див. §4. Коротко: машина A — `--shard 0/2`, машина B — `--shard 1/2`, той самий
конфіг; потім `experiment_merge.py <dirA> <dirB>`.

---

## 8. Швидкий чек «щось не так»

- `no venv found` → створи `.venv` (§7.1).
- `pong` не виводиться → движок не зібрано (§7.2) або не той `--engine-bin`.
- Ваги «не діють» → env читається на СТАРТІ движка; харнес виставляє їх перед
  спавном автоматично, тож перебудова не потрібна — але переконайся, що
  `--engine-bin` вказує на farm-здатний бінар (будь-який; farm — рантайм-прапор).
- Дуже повільно / `ms/move` високий → машина під навантаженням; для чистих
  чисел тримай її тихою (arm_20core_scaling §4.1 застереження про контамінацію).
- Порівнюєш профілі, а числа «стрибають» → перевір, що `seeds.base/count`
  однакові в усіх конфігах (парні сіди).

---

## 9. Speed benchmark — порівняння швидкості машин (Windows vs M4 Pro)

Окремий **фіксований** конфіг `config/experiments/benchmark.json` — щоб міряти
й порівнювати ЧИСТУ пропускну здатність заліза (ця Windows-машина vs MacBook
M4 Pro arm64), а не стратегію. Ваги/depth/сіди/stop_tile **однакові й
незмінні** на обох машинах (ваги = дефолти farm.go, як у `baseline.json`;
depth 6; `seeds base 1000 count 12`; stop_tile 2048). Змінюються тільки
**workers** — беруться з профілю машини (`windows` = cpu−2, `m4pro` = 13). Саме
ця різниця у воркерах і є вимірюваною реальною пропускною здатністю.

**Розмір** (`count 12`) підібраний під **~5 хв на цій Windows-машині у тихому
стані** (без інших farm-сим/HEUR прогонів). count 12 = рівно 2 гри/воркер на
windows (усі 6 воркерів, рівний `wall_s`); на M4 — 12 воркерів по 1 грі.

### Як запускати (той самий конфіг на обох)

```bash
cd project

# --- ТУТ, на Windows-машині: ---
scripts/experiment.sh config/experiments/benchmark.json --machine windows
# → research/experiments/benchmark__windows__shard0of1__<ts>/perf.json

# --- на MacBook M4 Pro (arm64): ---
scripts/experiment.sh config/experiments/benchmark.json --machine m4pro
# → research/experiments/benchmark__m4pro__shard0of1__<ts>/perf.json
```

> **Тримай машину ТИХОЮ** під час бенчмарку (жодних інших farm-сим/HEUR
> прогонів) — інакше `moves_per_sec`/`ms_per_move_mean` контамінуються (див. §8
> і arm_20core_scaling §4.1). НЕ міняй weights/depth/seeds/stop_tile — це зламає
> порівнюваність між машинами.

### Що порівнювати в `perf.json`

Однаковий обсяг ігор (12 однакових сідів, ті самі ваги/depth) на обох машинах →
пряме порівняння двох `perf.json`:

| поле | сенс | напрям |
|---|---|---|
| **`moves_per_sec`** | агрегатна пропускна здатність (усі воркери) | **більше = швидша машина** (головне число) |
| **`games_per_hour`** | скільки farm-ігор/год витягує машина | **більше = швидша** (головне число) |
| **`wall_s`** | стінний час на весь (однаковий) обсяг | **менше = швидша** |
| `ms_per_move_mean` | вартість одного ходу движка (mean, single-thread) | менше = швидший рушій/ядро |
| `workers` / `arch` | контекст: скільки воркерів, яка архітектура | — |

`moves_per_sec` і `games_per_hour` — головні числа порівняння; `wall_s` —
підтверджує (однаковий обсяг → менший `wall_s` = швидша машина). Оскільки M4 має
більше воркерів і швидший рушій, її прогін буде помітно коротший за 5 хв — це й є
очікуваний результат порівняння.

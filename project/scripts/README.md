# scripts/ — допоміжні скрипти проєкту

Усі запускаються з кореня repo (`monobank-2048`), якщо не сказано інше.

## Експерименти й статистика

| скрипт | призначення | запуск |
|---|---|---|
| `experiment.sh` | **harness тюнінгу farm-евристики**: конфіг → N farm-ігор паралельно (GOMAXPROCS=1, табу 2048) → самодостатня підпапка `research/experiments/<run-id>/`. Обгортка над `experiment_runner.py`. Формат конфігу + runbook для другої машини: `docs/experiments.md` | `bash project/scripts/experiment.sh project/config/experiments/baseline.json --machine windows` |
| `experiment_runner.py` | ядро harness (конфіг, шардинг сідів для 2 машин, паралельний прогін, агрегати, perf.json). Зазвичай через `experiment.sh` | `./.venv/Scripts/python.exe scripts/experiment_runner.py CONFIG.json [--machine m4pro] [--shard 0/2]` |
| `experiment_merge.py` | зливає кілька підпапок одного профілю (disjoint-шарди з різних машин) у спільний звіт; відмовляється зливати різні вагові профілі | `./.venv/Scripts/python.exe scripts/experiment_merge.py research/experiments/RUN_A research/experiments/RUN_B` |
| `arena.py` | **інференційна статистика і планування експериментів**: метрика — MOVES, цільова функція — хвіст `P(moves ≥ T)`. Підкоманди: `run` (грати руки, опційно з CRN), `stats` (µ/σ + fitted tail з bootstrap-CI, ранг за `z=(µ−T)/σ`), `compare` (парний A/B: t + Wilcoxon + CI, плюс F-тест на дисперсії), `corr` (валідація дешевого прокси проти дорогого прогону: Spearman/Kendall), `plan` (power: скільки сідів під заданий MDE; Monte-Carlo воронки відбору) | `./.venv/Scripts/python.exe scripts/arena.py stats --help` |

## Бенчмарки стратегій

| скрипт | призначення | запуск |
|---|---|---|
| `bench.sh` | прогін усіх стратегій на CLASSIC `SpawnModel` (P(4)=0.10) у `data/bench.db` з фіксованими сідами (base 1000) + sweep ваг depth-3 через `bench_weights.py`, у кінці — `bot2048 db report`. `data/` gitignored, БД не комітити | `bash scripts/bench.sh` (з `project/`) |
| `bench_weights.py` | драйвер тюнінгу ваг `ExpectimaxStrategy`: ганяє альтернативні `HeuristicWeights` напряму через публічний API (CLI хардкодить `DEFAULT_WEIGHTS`) і пише повну телеметрію — один експеримент на набір ваг | `./.venv/Scripts/python.exe scripts/bench_weights.py --games 60 --depth 3 --seed 1000 --db data/bench.db` |
| `docker-test.sh` | збирає тест-образ **нативно під архітектуру хоста** (той самий виклик працює на x86-64 і на ARM) і ганяє в ньому pytest. Усе після імені скрипта дописується до `pytest` | `scripts/docker-test.sh` · `scripts/docker-test.sh tests/core` · `IMAGE=bot2048-test:dev scripts/docker-test.sh` |

## Пристрій, ваги, сповіщення

| скрипт | призначення | запуск |
|---|---|---|
| `setup_adb.sh` | первинне налаштування ADB (git-bash; шляхи adb у лапках, `MSYS_NO_PATHCONV=1`) | `MSYS_NO_PATHCONV=1 bash project/scripts/setup_adb.sh` |
| `fetch_weights.sh` | завантажує ваги n-tuple-чемпіона (`champion-wall-snap-0316.bin`, 56 MB), яких **немає в git**, у `tools/ntuple/run/` і **верифікує розмір + md5** (битий файл видаляється, exit≠0). URL: `$BOT2048_WEIGHTS_URL`, інакше — останній GitHub-Release ассет, виведений з `origin` цього repo. Бот працює й без них: дефолтний движок (`tools/engine2048`) файла ваг не потребує, а Go-тести ntuple самі себе скіпають, коли мережі немає — ваги потрібні лише для record-движка і для живої гри на n-tuple-мережі | `scripts/fetch_weights.sh` (з `project/`) · `BOT2048_WEIGHTS_URL=https://... scripts/fetch_weights.sh` |
| `tg_send.ps1` | ручна відправка в Telegram: sendMessage, або sendPhoto (`-Form` у PS7; PS5.1 — фолбек на `python -m bot2048.tgnotify send`); токен лише в пам'яті процесу, URL не друкується; exit 0/1 | `powershell -File project\scripts\tg_send.ps1 "текст" [шлях_до_фото]` |

Python-обгортки Telegram-моста (те саме через `.venv`):

```powershell
# резервний вотчер (раннер мертвий -> ERROR/FATAL + heartbeat-watchdog):
project\.venv\Scripts\python.exe -m bot2048.tgnotify watch --root .

# одноразовий bootstrap TG_CHAT_ID (потрібен /start від @your_handle):
project\.venv\Scripts\python.exe -m bot2048.tgnotify bootstrap --root .

# ручна відправка (фолбек для tg_send.ps1 на PS 5.1):
project\.venv\Scripts\python.exe -m bot2048.tgnotify send "текст" [фото] --root .
```

Деталі: `docs/telegram-notify.md` (§5 команди, §6 основний/резервний шлях,
§8 безпека токена).

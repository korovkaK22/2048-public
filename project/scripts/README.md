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
| `setup_adb.sh` | первинне налаштування ADB + scrcpy, гілка по `uname -s`: macOS з Homebrew — `brew install --cask android-platform-tools` + `brew install scrcpy` (нічого в `tools/`, один adb на машині); Windows/Linux/mac без brew — завантажує platform-tools і scrcpy під свою ОС у `project/tools/` (gitignored) | `scripts/setup_adb.sh [--force]` (git-bash: `MSYS_NO_PATHCONV=1`) |
| `fetch_weights.sh` | завантажує ваги n-tuple-чемпіона (`champion-wall-snap-0316.bin`, 56 MB), яких **немає в git**, у `tools/ntuple/run/` і **верифікує розмір + md5** (битий файл видаляється, exit≠0). URL: `$BOT2048_WEIGHTS_URL`, інакше — останній GitHub-Release ассет, виведений з `origin` цього repo. Бот працює й без них: дефолтний движок (`tools/engine2048`) файла ваг не потребує, а Go-тести ntuple самі себе скіпають, коли мережі немає — ваги потрібні лише для record-движка і для живої гри на n-tuple-мережі | `scripts/fetch_weights.sh` (з `project/`) · `BOT2048_WEIGHTS_URL=https://... scripts/fetch_weights.sh` |
| `tg_send.sh` | ручна відправка в Telegram — тонка обгортка над `python -m bot2048.tgnotify send` (той сам транспорт/outbox, що й у раннера; токен лише в пам'яті процесу, URL не друкується); exit 0/1. Замінила `tg_send.ps1` | `scripts/tg_send.sh "текст" [шлях_до_фото]` |

## Live-хост (macOS основний; Windows/Linux теж працюють)

| скрипт | призначення | запуск |
|---|---|---|
| `live.sh` | **лаунчер живого раннера**: cd у `project/`, Homebrew в PATH, venv-python, `PYTHONPATH=src`, `MONO_PIN` з `../.secrets/device.env`, движок `tools/ntuple/ntuple` + ваги `champion-wall-snap-0316.bin` + важелі record-engine як env, `BOT2048_EVAL_MLP=0` на arm64, `BOT2048_SEARCH_WORKERS`=P-ядра на macOS, `caffeinate -is` на час життя процесу; далі `exec bot2048 live <play\|once\|research> <args>`. Env-ручки — тимчасово, до `config/strategies/champion-live.json` (StrategySpec) | `scripts/live.sh --strategy go --depth 4 --budget-ms 250 --capture apk --auto-login --games 0` · `scripts/live.sh once --capture apk` |
| `watch.sh` | лаунчер резервного Telegram-вотчера (`tgnotify watch --root .`) — окремий довгоживучий процес | `scripts/watch.sh` |
| `_host_env.sh` | спільний bootstrap для лаунчерів (**source**, не запускати): cd, PATH, `$PY`, `$PROJECT_DIR`, `source_secrets`, `die` | — |
| `macos/` | launchd-шаблони, `install.sh`, `setup.sh` для Mac mini — див. [`macos/README.md`](macos/README.md) і `docs/macos-setup.md` | `scripts/macos/setup.sh` → `scripts/macos/install.sh` |

Python-обгортки Telegram-моста напряму (те саме через `.venv`; `$PY` = `.venv/bin/python` або `.venv/Scripts/python.exe`):

```bash
# резервний вотчер (раннер мертвий -> ERROR/FATAL + heartbeat-watchdog):
$PY -m bot2048.tgnotify watch --root .          # або scripts/watch.sh

# одноразовий bootstrap TG_CHAT_ID (потрібен /start від @your_handle):
$PY -m bot2048.tgnotify bootstrap --root .

# ручна відправка:
$PY -m bot2048.tgnotify send "текст" [фото] --root .   # або scripts/tg_send.sh
```

Деталі: `docs/telegram-notify.md` (§5 команди, §6 основний/резервний шлях,
§8 безпека токена).

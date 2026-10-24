# scripts/ — допоміжні скрипти проєкту

Один підкаталог на компонент (власник і рецепт видалення — [`docs/components.md`](../../docs/components.md)):

| тека | компонент | що всередині |
|---|---|---|
| `sim/` | sim | `experiment.sh`, `experiment_runner.py`, `experiment_merge.py`, `arena.py`, `bench.sh`, `bench_weights.py` |
| `setup/` | setup (device + engines) | `setup_adb.sh`, `fetch_weights.sh` |
| `live/` | live | `live.sh`, `watch.sh`, `tg_send.sh`, `_host_env.sh` |
| `macos/` | macos-support | `setup.sh`, `install.sh`, launchd-шаблони — [`macos/README.md`](macos/README.md) |
| `dev/` | dev | `new-worktree.sh`, `rm-worktree.sh` |

Усі запускаються з `project/` (лаунчери самі `cd` куди треба), якщо не сказано інше.

## `sim/` — експерименти й статистика

| скрипт | призначення | запуск |
|---|---|---|
| `experiment.sh` | **harness тюнінгу farm-евристики**: конфіг → N farm-ігор паралельно (GOMAXPROCS=1, табу 2048) → самодостатня підпапка `research/experiments/<run-id>/`. Обгортка над `experiment_runner.py`. Формат конфігу + runbook для другої машини: `docs/experiments.md` | `bash project/scripts/sim/experiment.sh project/config/experiments/baseline.json --machine windows` |
| `experiment_runner.py` | ядро harness (конфіг, шардинг сідів для 2 машин, паралельний прогін, агрегати, perf.json). Зазвичай через `experiment.sh` | `./.venv/Scripts/python.exe scripts/sim/experiment_runner.py CONFIG.json [--machine m4pro] [--shard 0/2]` |
| `experiment_merge.py` | зливає кілька підпапок одного профілю (disjoint-шарди з різних машин) у спільний звіт; відмовляється зливати різні вагові профілі | `./.venv/Scripts/python.exe scripts/sim/experiment_merge.py research/experiments/RUN_A research/experiments/RUN_B` |
| `arena.py` | **інференційна статистика і планування експериментів**: метрика — MOVES, цільова функція — хвіст `P(moves ≥ T)`. Підкоманди: `run` (грати руки, опційно з CRN), `stats` (µ/σ + fitted tail з bootstrap-CI, ранг за `z=(µ−T)/σ`), `compare` (парний A/B: t + Wilcoxon + CI, плюс F-тест на дисперсії), `corr` (валідація дешевого прокси проти дорогого прогону: Spearman/Kendall), `plan` (power: скільки сідів під заданий MDE; Monte-Carlo воронки відбору) | `./.venv/Scripts/python.exe scripts/sim/arena.py stats --help` |

## `sim/` — бенчмарки стратегій

| скрипт | призначення | запуск |
|---|---|---|
| `bench.sh` | прогін усіх стратегій на CLASSIC `SpawnModel` (P(4)=0.10) у `data/bench.db` з фіксованими сідами (base 1000) + sweep ваг depth-3 через `bench_weights.py`, у кінці — `bot2048 db report`. `data/` gitignored, БД не комітити | `bash scripts/sim/bench.sh` (з `project/`) |
| `bench_weights.py` | драйвер тюнінгу ваг `ExpectimaxStrategy`: ганяє альтернативні `HeuristicWeights` напряму через публічний API (CLI хардкодить `DEFAULT_WEIGHTS`) і пише повну телеметрію — один експеримент на набір ваг | `./.venv/Scripts/python.exe scripts/sim/bench_weights.py --games 60 --depth 3 --seed 1000 --db data/bench.db` |

## `setup/` — пристрій і ваги; `live/tg_send.sh` — сповіщення

| скрипт | призначення | запуск |
|---|---|---|
| `setup_adb.sh` | первинне налаштування ADB + scrcpy, гілка по `uname -s`: macOS з Homebrew — `brew install --cask android-platform-tools` + `brew install scrcpy` (нічого в `tools/`, один adb на машині); Windows/Linux/mac без brew — завантажує platform-tools і scrcpy під свою ОС у `project/tools/` (gitignored) | `scripts/setup/setup_adb.sh [--force]` (git-bash: `MSYS_NO_PATHCONV=1`) |
| `fetch_weights.sh` | завантажує обидві n-tuple-мережі, яких **немає в git** (по 56 MB), у `tools/ntuple/run/`: `w-l0.003.bin` — **чемпіон, канонічна live-мережа** (`config/strategies/champion-live.json`; без закріпленої md5 — перевірка «бінарник правдоподібного розміру») і `champion-wall-snap-0316.bin` — wall-мережа для record-parity (`wall-record.json`; розмір + md5). Битий файл видаляється; кожна відсутня мережа — **явне повідомлення** (для `w-l0.003.bin` — як скопіювати з Windows-машини, якщо релізу нема), exit 1. URL: `$BOT2048_WEIGHTS_BASE_URL/<asset>`, `$BOT2048_WEIGHTS_URL` (лише wall), інакше — останній GitHub-Release цього `origin`. Сими працюють і без них (`tools/engine2048` ваг не потребує, Go-тести ntuple скіпаються); `live.sh` **вимагає** `w-l0.003.bin` і пінить через `BOT2048_NTUPLE_WEIGHTS` | `scripts/setup/fetch_weights.sh` (з `project/`) · `BOT2048_WEIGHTS_BASE_URL=https://.../dir scripts/setup/fetch_weights.sh` |
| `tg_send.sh` | ручна відправка в Telegram — тонка обгортка над `python -m bot2048.tgnotify send` (той сам транспорт/outbox, що й у раннера; токен лише в пам'яті процесу, URL не друкується); exit 0/1. Замінила `tg_send.ps1` | `scripts/live/tg_send.sh "текст" [шлях_до_фото]` |

## `live/` — live-хост (macOS основний; Windows/Linux теж працюють)

| скрипт | призначення | запуск |
|---|---|---|
| `live.sh` | **лаунчер живого раннера**: cd у `project/`, Homebrew в PATH, venv-python, `PYTHONPATH=src`, `MONO_PIN` з `../.secrets/device.env`, движок `tools/ntuple/ntuple` + ваги `w-l0.003.bin` (чемпіон = прод на i7; wall-мережа — через `BOT2048_NTUPLE_WEIGHTS=…`) + важелі record-engine як env, `BOT2048_EVAL_MLP=0` на arm64, `BOT2048_SEARCH_WORKERS`=P-ядра на macOS, `caffeinate -is` на час життя процесу; далі `exec bot2048 live <play\|once\|research> <args>`. Env-ручки — тимчасово, доки live-CLI не читає `config/strategies/champion-live.json` (StrategySpec) | прод-команда i7: `scripts/live/live.sh --strategy go --depth 4 --prefetch --capture apk --auto-login --games 0` · `scripts/live/live.sh once --capture apk` |
| `watch.sh` | лаунчер резервного Telegram-вотчера (`tgnotify watch --root .`) — окремий довгоживучий процес | `scripts/live/watch.sh` |
| `_host_env.sh` | спільний bootstrap для лаунчерів (**source**, не запускати): cd, PATH, `$PY`, `$PROJECT_DIR`, `source_secrets`, `die` | — |
| `macos/` | launchd-шаблони, `install.sh`, `setup.sh` для Mac mini — див. [`macos/README.md`](macos/README.md) і `docs/macos-setup.md` | `scripts/macos/setup.sh` → `scripts/macos/install.sh` |

## `dev/` — розробка

| скрипт | призначення | запуск |
|---|---|---|
| `new-worktree.sh` / `rm-worktree.sh` | git-worktree для паралельної роботи зі спільним venv (junction на Windows); прибирати ТІЛЬКИ `rm-worktree.sh` — див. `CLAUDE.md` | `project/scripts/dev/new-worktree.sh feat/x` · `project/scripts/dev/rm-worktree.sh feat/x` |

Python-обгортки Telegram-моста напряму (те саме через `.venv`; `$PY` = `.venv/bin/python` або `.venv/Scripts/python.exe`):

```bash
# резервний вотчер (раннер мертвий -> ERROR/FATAL + heartbeat-watchdog):
$PY -m bot2048.tgnotify watch --root .          # або scripts/live/watch.sh

# одноразовий bootstrap TG_CHAT_ID (потрібен /start від @your_handle):
$PY -m bot2048.tgnotify bootstrap --root .

# ручна відправка:
$PY -m bot2048.tgnotify send "текст" [фото] --root .   # або scripts/live/tg_send.sh
```

Деталі: `docs/telegram-notify.md` (§5 команди, §6 основний/резервний шлях,
§8 безпека токена).

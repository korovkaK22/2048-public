# Компоненти — маніфест і рецепти видалення

> Мета (слова власника): «стандартизувати все, щоб коли щось не потрібно — його
> можна було швидко видалити». Кожен компонент живе у своїй теці, має **один**
> запис тут, і тест гарантує, що ніхто сторонній від нього не залежить.
>
> Правило: **новий компонент = нова тека + запис у цьому файлі + контракт
> import-linter** (`project/pyproject.toml → [tool.importlinter]`). Зміна меж —
> спершу тут, потім у контракті, потім у коді.

Джерело істини про залежності — не ця таблиця, а скрипт:

```bash
cd project && $PY scripts/dev/import_graph.py        # [--dot | --json]
```

Він обходить `src/bot2048` через `ast` (нічого не імпортує) і друкує кожне ребро
між підпакетами з типом: `static` (на рівні модуля), `lazy` (усередині функції),
`typing` (під `if TYPE_CHECKING:`). Контракти в `pyproject.toml` виведені з його
виводу, і тест `tests/test_architecture.py` (import-linter) тримає їх зеленими:
`pytest` і CI падають на новому імпорті через межу.

## 1. Карта залежностей (стан 2026-10-25)

Ребра між підпакетами `bot2048` (static / lazy / typing). Низ таблиці — найглибший шар.

| звідки → куди | static | lazy | typing | де саме (lazy/typing) |
|---|---:|---:|---:|---|
| `ai → core` | 8 | | | |
| `analysis → core` | 2 | | | |
| `analysis → storage` | 1 | 1 | | `analysis.render:44` (`board_from_hex`) |
| `storage → analysis` | 1 | | | `storage/cli.py` (render для `db replay`) |
| `storage → core, recording` | 3+2 | | | |
| `recording → core` | 1 | | | |
| `vision → core` | 4 | | | |
| `ondevice → core, vision` | 2+2 | | | sampler — Python-референс парсера |
| `sim → ai, core, recording` | 4+5+2 | | | |
| `sim → storage` | | 1 | | `sim/cli.py:100` (`--db` опційний) |
| `device → core` | 4 | | | |
| `device → vision` | 4 | | | `device/crop.py` (не підключений) + `supervisor.py` |
| `device → ondevice` | 2 | | | `device/apk_capture.py` |
| `device → storage` | 2 | | | `device/supervisor.py` |
| **`device → runner`** | **3** | | | **`device/supervisor.py` — борг, див. §4** |
| `runner → device` | 31 | | | |
| `runner → vision` | 14 | | | |
| `runner → storage` | 13 | | | |
| `runner → core` | 14 | | 1 | |
| `runner → ai` | 8 | | 1 | |
| `runner → recording` | 6 | | | |
| `runner → tgnotify` | 3 | | | `live.py`, `research.py` (`PAUSE_FLAG_PATH`), `cli/guard.py` (`get_notifier`) |
| `runner → logging_setup` | 1 | | | `cli/guard.py` |
| `__main__ → cli` | 1 | | | `cli.py` реєструє саб-апи через `importlib` — статичних ребер не має |

Що з цього випливає:

- **`tgnotify` не імпортує нічого з `bot2048`** — чистий stdlib-міст; видаляється без
  наслідків для решти (крім трьох імпортів у `runner`).
- **`vision`, `ai` залежать лише від `core`.** `ondevice` — від `core` + `vision`.
- **`storage` і `analysis` взаємно залежні** (render ↔ hex-кодек, `db replay`) — це один
  компонент, а не два; у контракті вони на одному шарі (`storage : analysis`).
- **`sim` не знає про телефон**: `ai`, `core`, `recording`, `storage` — усе.
- **Єдиний цикл у графі — `device ↔ runner`**, і весь він у `device/supervisor.py`.
- `cli.py` — корінь: саб-апи `sim / db / vision / live` підхоплюються лениво, тож
  видалення будь-якого з них не ламає `bot2048 --help`.

## 2. Шари (контракт `component layers`)

Модуль може імпортувати все **нижче** свого рядка і нічого вище. `|` — незалежні
сусіди (не імпортують одне одного), `:` — сусіди, яким можна.

```
bot2048.cli
bot2048.runner                                  live
bot2048.device                                  device
bot2048.ondevice                                ondevice
bot2048.sim | bot2048.vision | bot2048.tgnotify sim · vision · tgnotify
bot2048.ai                                      ai
bot2048.storage : bot2048.analysis              core (сховище)
bot2048.recording | bot2048.logging_setup       core (контракти)
bot2048.core                                    core
```

Плюс п'ять `forbidden`-контрактів для листків (що саме їм дозволено) — у
`pyproject.toml`, по одному на компонент.

## 3. Компоненти

Позначки: **ядро** — не видаляється; **видаляється** — рецепт унизу запису;
**видаляється разом із …** — без названих компонентів сенсу не має.

### `core` — ядро
- **Призначення:** правила гри (бітборд, детермінований движок), контракти подій,
  SQLite-телеметрія, рендер/статистика для `db`-команд, structlog.
- **Шляхи:** `src/bot2048/core/`, `recording.py`, `logging_setup.py`, `storage/`,
  `analysis/`, `cli.py`, `__main__.py`; `tests/core/`, `tests/storage/`
  (включно з `test_analysis.py`, `test_logging_setup.py`), `tests/conftest.py`.
- **Входи:** `bot2048 db report | replay | spawn-stats | latency`.
- **Залежності:** `numpy`, `structlog`, `typer`, `rich` (дефолтна інсталяція).
- **Дозволені внутрішні імпорти:** `storage/analysis → core, recording`; `core`,
  `recording`, `logging_setup` — нічого з `bot2048`.
- **Заморожені контракти:** `core/types.py`, `recording.py` — не міняти без узгодження.
- **CI:** job `python` (ruff, mypy, pytest).
- **Видалення:** не передбачено.

### `ai` — стратегії і міст до Go-рушіїв
- **Призначення:** `Strategy`/`Decision`, Python-expectimax (референс), `go_engine.py`
  (stdin-протокол до `tools/*`, автозбірка, prefetch), `spec.py` (`StrategySpec`).
- **Шляхи:** `src/bot2048/ai/`; `tests/ai/` (+`fixtures/expectimax_depth_golden.json`);
  `config/strategies/*.json`.
- **Входи:** немає власних CLI; споживачі — `sim`, `live`.
- **Залежності:** дефолтна інсталяція; Go ≥ 1.24 на PATH для автозбірки рушія
  (без Go тести Go-стратегії скіпаються).
- **Дозволені внутрішні імпорти:** тільки `core`.
- **Видалення:** лише разом із `sim` і `live` (обидва імпортують `ai`). Окремо
  видаляється лише `ai/go_engine.py` + `tests/ai/test_go_engine*.py` + `tests/runner/
  test_cli_binary_resolution.py` (якщо прибирати обидва Go-рушії) — тоді
  `runner/cli/strategy.py` і `sim/cli.py` втрачають варіант `go`.

### `sim` — симулятор і батч-експерименти — **видаляється**
- **Призначення:** `play_game(s)` з повним записом подій, телеметрія «чому партія
  померла», харнес farm-експериментів, статистика/планування (`arena.py`), бенчмарки.
- **Шляхи:** `src/bot2048/sim/`; `scripts/sim/` (`experiment.sh`, `experiment_runner.py`,
  `experiment_merge.py`, `arena.py`, `bench.sh`, `bench_weights.py`);
  `config/experiments/*.json`; `research/experiments/<run-id>/` (результати);
  `tests/sim/`, `tests/scripts/`; `docs/experiments.md`, `docs/experiment-log.md`
  (журнал), `docs/tail-analysis.md`.
- **Входи:** `bot2048 sim run | bench`; `scripts/sim/experiment.sh CONFIG.json`.
- **Залежності:** дефолтна інсталяція (+ Go для `--strategy go-expectimax`).
- **Дозволені внутрішні імпорти:** `ai`, `core`, `recording`, `storage`/`analysis`
  (`storage` — lazy, лише з `--db`).
- **CI:** частина job `python`.
- **Хто залежить:** `tests/ai/test_expectimax.py`, `tests/ai/test_go_engine.py`
  (ганяють стратегії через `sim.runner.play_game`) — більше ніхто в `src`.
- **Рецепт видалення:** `git rm -r src/bot2048/sim scripts/sim config/experiments
  tests/sim tests/scripts docs/experiments.md`; у `cli.py` прибрати рядок
  `("sim", "bot2048.sim.cli")`; у `pyproject.toml` — рядок `bot2048.sim` з `layers`
  і контракт `sim knows no phone`; переписати два тести в `tests/ai/` на власний
  міні-цикл (або лишити `sim/runner.py` як частину `ai`); з `docs/README.md`,
  `CLAUDE.md`, `README.md` зняти згадки `bot2048 sim` / `experiment.sh`.
  `research/experiments/` — дані, вирішити окремо (`research`).

### `vision` — кадр → `Board`
- **Призначення:** шаблони цифр + палітра, калібрування, стани екрана.
- **Шляхи:** `src/bot2048/vision/`; `data/calibration.json`; `tests/vision/`
  (+`fixtures/*.png|jpg`), `tests/imgcache.py`; `docs/device-ui-notes.md` (спільно з
  `device`).
- **Входи:** `bot2048 vision calibrate | parse | state | add-glyphs`.
- **Залежності:** `numpy` (ядро); `cv2`/`PIL` — лише в CLI для читання файлів
  (екстра `[device]`, імпорт лениво).
- **Дозволені внутрішні імпорти:** тільки `core`.
- **Хто залежить:** `runner` (14), `ondevice` (sampler-референс), `device`
  (`crop.py` — мертвий код; `supervisor.py`).
- **Видалення:** лише разом із `live`, `ondevice`; окремо — ні.

### `ondevice` — протокол APK-семплера — **видаляється**
- **Призначення:** 22-байтний пакет дошки (CRC16, resync), Python-референс семплера,
  демультиплексор; Kotlin-порт на телефоні.
- **Шляхи:** `src/bot2048/ondevice/`; `ondevice-apk/` (Gradle, `gradlew`);
  `tests/ondevice/`; `docs/ondevice-vision-design.md`; `.gitignore` блок
  `project/ondevice-apk/**`, `.gitattributes` `*.apk`.
- **Входи:** `bot2048 live … --capture apk`.
- **Залежності:** Python — нічого зверх ядра; APK — Android SDK 34, Gradle 8.14, JBR 21
  (локально, не в CI).
- **Дозволені внутрішні імпорти:** `core`, `vision`.
- **Хто залежить:** `device/apk_capture.py` (+ `tests/device/test_apk_capture.py`,
  `tests/runner/test_live_apk.py`, гілка `apk` у `runner/cli/capture.py`).
- **Рецепт видалення:** `git rm -r src/bot2048/ondevice ondevice-apk tests/ondevice
  docs/ondevice-vision-design.md src/bot2048/device/apk_capture.py
  tests/device/test_apk_capture.py tests/runner/test_live_apk.py`; у
  `runner/cli/capture.py` прибрати варіант `apk`; у `pyproject.toml` — рядок
  `bot2048.ondevice` з `layers` і контракт `ondevice depends on …`; `.gitignore`/
  `.gitattributes` рядки про APK; `scripts/macos/com.bot2048.live.plist.template`
  і `docs/*` — замінити `--capture apk` на `screencap`. Ціна: ~650 мс капчер замість
  ~25 мс (`docs/experiment-log.md`).

### `device` — драйвер телефона
- **Призначення:** ADB з таймаутами, три бекенди захоплення, humanizer, постійний
  `adb shell` для свайпів, екран авторизації (PIN лише з env), `ui_map`.
- **Шляхи:** `src/bot2048/device/` (**крім** `supervisor.py` — див. §4);
  `scripts/setup/setup_adb.sh`; `tests/device/`; `docs/live-setup.md` (§adb/scrcpy),
  `docs/device-ui-notes.md`; `tools/platform-tools/`, `tools/scrcpy/`, `tools/ffmpeg/`
  (gitignored, кладе `setup_adb.sh`).
- **Входи:** немає власних CLI (усе через `bot2048 live`).
- **Залежності:** екстра `[device]` = `adbutils`, `opencv-python`, `pillow`; хост:
  adb, scrcpy, ffmpeg (brew на macOS / `tools/` на Windows).
- **Дозволені внутрішні імпорти:** `core`, `vision`, `ondevice`, `storage`
  (`storage` — лише `supervisor.py`).
- **Хто залежить:** `runner` (31), `research/scripts/*.py`.
- **Видалення:** лише разом із `live`.

### `live` — живий цикл — **видаляється**
- **Призначення:** `LiveRunner` + співробітники (верифікація ходу, драбина бюджету,
  ескалація, відновлення сесії, інциденти, heartbeat, lifecycle), research-режим,
  `bot2048 live` CLI, лаунчери.
- **Шляхи:** `src/bot2048/runner/`; `src/bot2048/device/supervisor.py` (борг, §4);
  `scripts/live/` (`live.sh`, `watch.sh`, `tg_send.sh`, `_host_env.sh`);
  `tests/runner/`; `docs/ops-playbook.md`, `docs/ops-monitoring.md`,
  `docs/production-state.md`, `docs/edge-cases.md`, `docs/rules-research.md`,
  `docs/live-setup.md` (§run); `logs/`, `control/` (рантайм, gitignored).
- **Входи:** `bot2048 live play | once | research`; `scripts/live/live.sh`;
  launchd `com.bot2048.live` (`macos-support`).
- **Залежності:** екстра `[live]` (= `[device]`); секрети `.secrets/device.env`;
  рушій `tools/ntuple/ntuple` + ваги (`engines/ntuple`).
- **Дозволені внутрішні імпорти:** усе, крім `cli` (верхній шар).
- **Хто залежить:** ніхто в `src` (крім `device/supervisor.py` — борг);
  `tests/storage/test_async_recorder.py:318` імпортує `runner.live.LiveConfig`
  в одному тесті.
- **CI:** job `python`; job `macos` (рендер launchd-шаблонів).
- **Рецепт видалення:** `git rm -r src/bot2048/runner src/bot2048/device/supervisor.py
  scripts/live tests/runner tests/device/test_supervisor.py docs/ops-playbook.md
  docs/ops-monitoring.md docs/production-state.md docs/edge-cases.md`;
  `cli.py` — прибрати `("live", "bot2048.runner.cli")`; `pyproject.toml` — екстра
  `live`, рядок `bot2048.runner` з `layers`, блок `ignore_imports`;
  `tests/storage/test_async_recorder.py` — один тест із `LiveConfig`;
  `scripts/macos/*` (`macos-support`) і `tgnotify`'s `watch.sh` втрачають сенс —
  видаляти разом; `CLAUDE.md`, `docs/README.md`, `README.md` — секції про live.

### `tgnotify` — Telegram-міст — **видаляється**
- **Призначення:** алерти, heartbeat-watchdog, інцидент-бандли, команди
  `/status /pause /resume`, офлайн-outbox; окремий процес-вотчер.
- **Шляхи:** `src/bot2048/tgnotify/`; `tests/tgnotify/`; `docs/telegram-notify.md`;
  `scripts/live/watch.sh`, `scripts/live/tg_send.sh`;
  `scripts/macos/com.bot2048.watch.plist.template`; `.secrets/telegram.env`
  (поза git; `.env.example`).
- **Входи:** `python -m bot2048.tgnotify watch | send | bootstrap`; `scripts/live/
  watch.sh`, `tg_send.sh`; launchd `com.bot2048.watch`.
- **Залежності:** **тільки stdlib** (`urllib`) — навмисно, щоб міст жив на будь-якому
  Python.
- **Дозволені внутрішні імпорти:** жодних (контракт `tgnotify is stdlib-only`).
- **Хто залежить:** `runner/live.py`, `runner/research.py` (`PAUSE_FLAG_PATH`),
  `runner/cli/guard.py` (`get_notifier`).
- **Рецепт видалення:** `git rm -r src/bot2048/tgnotify tests/tgnotify
  docs/telegram-notify.md scripts/live/watch.sh scripts/live/tg_send.sh
  scripts/macos/com.bot2048.watch.plist.template`; у `runner` замінити три імпорти
  (константа шляху прапорця + no-op notifier); `pyproject.toml` — `bot2048.tgnotify`
  у `layers` і контракт `tgnotify is stdlib-only`; `scripts/macos/install.sh` —
  масив `labels`; `.env.example` — `TG_*`; `CLAUDE.md`/`docs/README.md`.

### `engines/engine2048` — Go: baseline-рушій
- **Призначення:** бітборд + expectimax (`cprob`, TT), ручна евристика «farm»
  (18 ваг через env). Самодостатній, без файлів даних — відтворюваний baseline і
  дефолт автозбірки з `ai/go_engine.py`.
- **Шляхи:** `tools/engine2048/` (go.mod, `build.sh`); `ai/go_engine.py` (міст,
  спільний з `ntuple`); `tests/ai/test_go_engine*.py`, `tests/runner/
  test_cli_binary_resolution.py`.
- **Входи:** `--strategy go-expectimax` (sim), `--strategy go` (live);
  `BOT2048_ENGINE2048_BIN`.
- **Залежності:** Go ≥ 1.24 (stdlib-only модуль).
- **CI:** job `go` (matrix `engine2048`), job `macos` (vet/test -short/build).
- **Видалення:** можливе лише з перемиканням дефолту `go_engine.py` на `ntuple`
  (шлях бінарника за замовчуванням + автозбірка) — інакше `sim`/`live` без Go-стратегії.
  `git rm -r tools/engine2048`; CI matrix; `docs/architecture.md` §Go-рушії;
  `config/experiments/*` (усі farm-конфіги — на цей рушій) → разом із `sim`-харнесом.

### `engines/ntuple` — Go: n-tuple мережа, record-engine — **видаляється**
- **Призначення:** оцінка n-tuple мережею (TD), leaf-cache, режим полювання на рекорд,
  підкоманди `train / probe / bench / perfbench / arena`. Робочий рушій прод-гри.
- **Шляхи:** `tools/ntuple/` (go.mod, `BUILD.md`); `tools/ntuple/run/*.bin`
  (ваги, gitignored, GitHub Release); `scripts/setup/fetch_weights.sh`;
  `config/strategies/{champion-live,champion-l0003,wall-record}.json`;
  `docs/record-engine.md`; блок engine-env у `scripts/live/live.sh`.
- **Входи:** `BOT2048_ENGINE2048_BIN=tools/ntuple/ntuple` (той самий протокол);
  `BOT2048_NTUPLE_WEIGHTS`, `BOT2048_RECORD_ENGINE=1`.
- **Залежності:** Go ≥ 1.24; 56 МБ ваг.
- **CI:** job `go` (matrix `ntuple`; тести з вагами скіпаються), job `macos`.
- **Рецепт видалення:** `git rm -r tools/ntuple scripts/setup/fetch_weights.sh
  docs/record-engine.md config/strategies/{champion-live,champion-l0003,wall-record}.json`;
  CI matrix; `scripts/live/live.sh` — блок `engine binary + weights` і `record engine`
  (рушій стає `tools/engine2048`); `scripts/macos/setup.sh` кроки 3–4; `.gitignore`
  (`*.bin`, `*.pgo`, `project/tools/ntuple/run/`); `docs/production-state.md`,
  `docs/macos-setup.md` §3, `README.md` — результати лишаються як історія.
  **Ціна:** втрата чемпіона (+185 ходів медіани проти farm).

### `research` — одноразові дослідження — **видаляється**
- **Призначення:** скрипти-заміри, сирі дані, датовані звіти, знімок агентської пам'яті.
- **Шляхи:** `project/research/` (`scripts/`, `early_spawn/`, `hazard_k5/`,
  `live-matches/`, `experiments/` — результати `sim`-харнесу); `docs/research/`,
  `docs/knowledge/`, `docs/archive/`.
- **Залежності:** скрипти імпортують `bot2048.device`, `vision`, `core` (не навпаки).
- **Хто залежить:** ніхто (`docs/experiment-log.md` і `README.md` посилаються як на
  джерела).
- **Рецепт видалення:** `git rm -r project/research docs/research docs/knowledge
  docs/archive`; `docs/README.md` — секції «Дослідження», «Архів», «Знання»;
  `CLAUDE.md` — рядки `docs/knowledge/`, `docs/research/`, `docs/archive/`;
  `.gitignore` — `project/research/**/raw/`; `.gitattributes` не чіпати.
  Нічого в `src`/`tests` не ламається.

### `macos-support` — Mac mini як live-хост — **видаляється**
- **Призначення:** бутстрап хоста (brew, venv, arm64-рушії, ваги), launchd-агенти,
  runbook.
- **Шляхи:** `scripts/macos/` (`setup.sh`, `install.sh`, два plist-шаблони, README);
  `docs/macos-setup.md`; гілки `is_macos`/`host_arch` у `scripts/live/_host_env.sh`,
  `caffeinate`/`sysctl`/`EVAL_MLP` у `scripts/live/live.sh`; brew-гілка в
  `scripts/setup/setup_adb.sh`; CI job `macos`; `CLAUDE.md` §Середовище (macOS).
- **Залежності:** Homebrew; macOS-14 раннери в CI (платні, тільки push/nightly).
- **Рецепт видалення:** `git rm -r scripts/macos docs/macos-setup.md`; CI — job
  `macos`; `_host_env.sh`/`live.sh`/`setup_adb.sh` — прибрати macOS-гілки (скрипти
  лишаються POSIX); `docs/README.md`, `CLAUDE.md`, `docs/live-setup.md` — згадки.

### `windows-support` — Windows 10 як дев-/резервний хост — **видаляється**
- **Призначення:** Git-Bash-сумісність лаунчерів, Windows-гілки у скриптах, Windows-
  шляхи в доках. Це **профіль**, не тека: Windows-специфіка розсипана по умовах.
- **Шляхи:** `.venv/Scripts/python.exe` гілки в `scripts/live/_host_env.sh`,
  `scripts/sim/experiment.sh`, `scripts/sim/bench.sh`; `MINGW*` гілки в
  `scripts/live/live.sh`, `scripts/setup/setup_adb.sh`; junction-гілка (`mklink /J`,
  `cygpath`) у `scripts/dev/new-worktree.sh`, `rm-worktree.sh`; `cygpath -m` у
  `_host_env.sh`; `.exe`-суфікси в `ai/go_engine.py`, `runner/cli/tools.py`;
  `tools/engine2048/cpu_time_windows_test.go`; `ondevice-apk/gradlew.bat`;
  `CLAUDE.md` §Середовище (Windows), `docs/ops-playbook.md` (`taskkill`).
- **Рецепт видалення:** прибрати названі гілки (кожна — `if`/`case` на `uname`),
  `gradlew.bat`, `cpu_time_windows_test.go` (власник — `engines/engine2048`),
  згадки в доках. `src` змінюється лише в двох місцях (`.exe`-суфікс).

### `ci` — автоматичні перевірки
- **Шляхи:** `.github/workflows/ci.yml` (jobs `python`, `go`×2, `macos`);
  `tests/test_architecture.py` + `[tool.importlinter]`, `[tool.ruff]`, `[tool.mypy]`,
  `[tool.pytest.ini_options]` у `pyproject.toml`; екстра `dev`; `tests/conftest.py`
  (xdist-групи).
- **Залежності:** GitHub Actions; `import-linter`, `ruff`, `mypy`, `pytest(-xdist)`.
- **Видалення:** `.github/` — без наслідків для коду; `test_architecture.py` + блок
  `[tool.importlinter]` + `import-linter` з `dev` — межі перестають перевірятись.

### `dev` — інструменти розробника
- **Шляхи:** `scripts/dev/` (`new-worktree.sh`, `rm-worktree.sh`, `import_graph.py`);
  `CLAUDE.md`, `.claude/agents/`; `.gitattributes`.
- **Видалення:** без наслідків для коду; `import_graph.py` — джерело для цього файлу.

## 4. Відкриті борги меж

| борг | де | чому не виправлено зараз | план |
|---|---|---|---|
| `device/supervisor.py → runner.{alerts,escalation,incidents}` — єдиний цикл графа | `ignore_imports` у `pyproject.toml` (3 ребра) | supervisor — FSM живого прогону (ESCALATION/FATAL, ops-БД), а не драйвер; перенесення зачіпає 9 імпортерів у `runner` + `tests/runner/conftest.py` + `tests/device/test_supervisor.py` — не для chore-гілки без прогону на живому телефоні | `git mv device/supervisor.py runner/supervisor.py`, перенести тест, прибрати `ignore_imports`; тоді `device` → `core, vision, ondevice` чисто |
| `device/crop.py` — не підключений, тягне `vision` | `device/` | мертвий код, зафіксований як негативний результат (`architecture.md` §Стан) | або видалити разом із `tests/device/test_crop.py`, або лишити — на межу не впливає (`device → vision` дозволено) |
| `tests/storage/test_async_recorder.py` імпортує `runner.live.LiveConfig` | один тест | тест ядра спирається на конфіг live | винести потрібні поля у фікстуру тесту |
| `tools/` змішує наш Go-код і завантажені бінарники (`platform-tools/`, `scrcpy/`, `ffmpeg/`) | `project/tools/` | паралельна гілка редагує `tools/engine2048`, `tools/ntuple` | **пропозиція:** `project/engines/{engine2048,ntuple}` для Go-модулів; `tools/` лишити тільки під gitignored завантаження `setup_adb.sh`. Торкається `go_engine.py` (шлях за замовчуванням), `live.sh`, `fetch_weights.sh`, `setup.sh`, CI `working-directory`, `.gitignore`, доки |
| `scripts/setup/` — тека під два компоненти | `setup_adb.sh` (device), `fetch_weights.sh` (engines/ntuple) | обидва — «поставити на хост», зручно поруч | лишити; власник кожного файлу — у цьому маніфесті |

## 5. Як додати компонент

1. Тека в `src/bot2048/<name>/` + дзеркало `tests/<name>/` (+ `scripts/<name>/`, якщо є
   скрипти; `docs/<name>-*.md`, якщо є дока).
2. Запис у §3 за тим самим шаблоном: призначення → шляхи → входи → залежності →
   дозволені внутрішні імпорти → CI → хто залежить → **рецепт видалення**.
3. `pyproject.toml → [tool.importlinter]`: рядок у `layers` (де в стеку) і, для листка,
   `forbidden`-контракт. Запустити `$PY scripts/dev/import_graph.py` і
   `lint-imports --config pyproject.toml`; `pytest tests/test_architecture.py`.
4. Якщо є екстра — `[project.optional-dependencies]` з коментарем «component `<name>`».
5. `cli.py` — саб-апп реєструється через `_SUBAPPS` (лениво).

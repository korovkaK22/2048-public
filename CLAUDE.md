# monobank-2048 — бот для гри 2048 у застосунку monobank (Android)

Пайплайн: скріншот (ADB) → розпізнавання дошки по кольорах → expectimax → humanized свайп → повний запис у SQLite (кожен хід/спавн/рішення відтворюється постфактум).

## Документація (тримаємо чистою; нове — у профільний файл, не сюди)
- `docs/production-state.md` — що реально грає в проді (мережа, глибина, record engine), результати, висновок про стелю. Читати перед будь-якою зміною стратегії.
- `docs/README.md` — **індекс усіх доків** (по рядку «що там / коли читати»). Новий док — додати туди.
- `docs/architecture.md` — **головний документ**: layout, контракти модулів, схема БД, фази. Читати першим.
- `docs/components.md` — **маніфест компонентів**: граф імпортів, шари, для кожного компонента — шляхи, входи, залежності, хто від нього залежить і **рецепт видалення**. Межі тримає `tests/test_architecture.py` (import-linter). Читати перед тим, як чіпати структуру чи щось видаляти.
- `docs/device-ui-notes.md` — усі нюанси UI телефона і гри (палітра, безпечні зони, баги застосунку). Нові спостереження про телефон/гру писати ТУДИ.
- `docs/live-setup.md` — підключення телефона, ADB, зняття скріншотів.
- `docs/rules-research.md` — відкриті питання про правила клону і як їх міряємо.
- `docs/ops-playbook.md` — операційні граблі: як правильно запускати Monitor, вбивати фонові процеси, ADB-по-WiFi, CLI/venv/тести. Читати перед живими прогонами.
- `docs/training-and-runs-census.md` — **зведення по всій роботі**: що натренували (архітектура, гіперпараметри, curriculum), скільки епізодів/прогонів, швидкості (eps/s, ходів/с), і таблиця «що зайшло / що закрили».
- `docs/knowledge/` — snapshot вивантаженої агентської пам'яті (2026-07-29): висновки про стелю скору, закриті глухі кути, операційні правила. Історичний дамп, не актуальна дока — див. `docs/knowledge/README.md`.
- `docs/research/` — датовані дослідницькі звіти (`.md`); скрипти й сирі дані до них лишаються в `project/research/`. Новий звіт — сюди.
- `docs/archive/` — витіснені/виконані документи; кожен з приміткою «чому в архіві» в `docs/archive/README.md`.

## Середовище
Два хости. **Live (телефон, launchd) і бектести — Mac mini M4 (macOS, arm64) — основний**;
Windows 10 i7 — дев-машина й резервний live-хост. Скрипти репо мусять іти на обох
(POSIX bash, `set -euo pipefail`, LF) — тому `project/scripts/*.sh`, не `.ps1`.
- **macOS (Mac mini M4, 4P+6E, 16 ГБ)** — runbook `docs/macos-setup.md`. Інструменти з Homebrew
  (`/opt/homebrew/bin`: adb-cask, scrcpy, ffmpeg, go, python@3.12); venv `project/.venv/bin/python`;
  рушії — нативні arm64 (`go build -pgo=auto`, без `GOAMD64`). Лаунчери: `scripts/live/live.sh`
  (ставить env рушія: `w-l0.003.bin` + record-engine, `EVAL_MLP=0`, воркери = P-ядра,
  `caffeinate`), `scripts/live/watch.sh`; супервізія — `scripts/macos/install.sh` (launchd).
  Стоп — `kill -TERM` (→ `exit_reason='killed'`). Чексуми рішень ≠ amd64 (FMA) — це норма.
- **Windows 10** — команди через Bash (Git Bash). Python 3.12 (`py -3.12`); venv `project/.venv`
  (`./.venv/Scripts/python.exe`). adb: `project/tools/platform-tools/adb.exe` (gitignored,
  `scripts/setup/setup_adb.sh`). Процеси вбивати `taskkill.exe //PID <WINPID> //T //F`.
- З `project/` на будь-якому хості (`$PY` = `.venv/bin/python` або `.venv/Scripts/python.exe`):
  `$PY -m pytest`, `$PY -m ruff check src tests`, `$PY -m mypy src`, CLI — `$PY -m bot2048.cli`
  (у worktree — саме так, не `.venv/*/bot2048`: той editable з головної копії).
- Тимчасові файли (тестові скріншоти, чернетки, одноразові дампи) — у `tmp/` в КОРЕНІ репозиторію-папки (`C:\Users\sa095\Desktop\monobank-2048\tmp\`, поза git). Не смітити в `data/` чи `project/`.
- Docker у проєкті немає (прибрано 2026-10): і live, і батч-симуляції — нативно на хості.

## Git та робочий процес
- **Репозиторій**: git у КОРЕНІ (`C:\Users\sa095\Desktop\monobank-2048`), `origin` → https://github.com/korovkaK22/monobank-2048.
- **Секрети НІКОЛИ не комітяться**: `.secrets/`, `*.env`, БД (`*.db`), бінарники тулів (adb/ffmpeg/scrcpy/`*.exe`/`*.dll`), raw-медіа — у `.gitignore`. Секрети лише з env; `.env.example` перелічує змінні (TG_BOT_TOKEN/TG_CHAT_ID/TG_USER_HANDLE у `.secrets/telegram.env`, MONO_PIN у `.secrets/device.env`).
- **Кожна фіча = окрема гілка + окремий коміт + пуш.** Не змішувати фічі в одному коміті.
- **Формат коміт-меседжа** (щоб з `git log` орієнтуватись без діфа): subject — коротко ЩО (imperative, ≤72 симв.). Body — кількома рядками **ЩО** зроблено, **ЯК** (підхід/ключова зміна) і **ЧОМУ** (яку проблему рішає, чому саме так). Приклади в історії: `feat: fast swipe … / FAST profile default, 80ms … / ~220-340ms/move saved`.
- **Лог експериментів**: кожен випробуваний варіант (евристика / набір ваг / depth / стратегія) → короткий запис у `docs/experiment-log.md` (config → результат → чому/висновок, найновіші зверху). Щоб не переганяти глухі кути й бачити всю історію того, що вже пробували і чим воно скінчилось.
- **Гілки**: `feat/…`, `fix/…`, `refactor/…`, `research/…`, `docs/…`, `chore/…`. У `main` — тільки мерж зеленої гілки (ruff + mypy + pytest).
- **Паралельна робота = git-worktree.** `project/scripts/dev/new-worktree.sh <гілка>` → `worktrees/<гілка>/` (у `.gitignore`) з junction на спільний `project/.venv`. Прибирати ТІЛЬКИ `project/scripts/dev/rm-worktree.sh <гілка>` (знімає junction першим; голий `git worktree remove --force`/`rm -rf` може пройти крізь junction і знести головний venv). У worktree CLI — `PYTHONPATH=src ./.venv/Scripts/python -m bot2048`, не `./.venv/Scripts/bot2048` (той editable з головної копії).
- **Новий компонент = нова тека + запис у `docs/components.md` + контракт import-linter** (`pyproject.toml → [tool.importlinter]`). Нічого «поруч» в чужій теці; скрипти — у `project/scripts/<компонент>/`. `pytest` падає на імпорті через межу.
- **Зони (file ownership)** — одна паралельна задача = одна зона: `vision/` · `ai/` + `tools/engine2048|ntuple` · `device/` · `runner/` · `storage/` · `sim/` + `analysis/` · `ondevice/` + `ondevice-apk/` · `tgnotify/` · `docs/`. Зачіпаєш чужу зону — це окрема гілка або узгодження.
- **`docs/experiment-log.md`** мержиться як `merge=union` (`.gitattributes`) — записи з паралельних гілок не конфліктують; після мержу глянути, що блоки цілі.
- **Головна сесія = оркестратор**: декомпозує, запускає агентів, приймає результати, **мержить гілки в main і пушить** пофіче.
- **Агенти працюють у власних гілках/worktree** (isolation) — там створюють і пишуть, тому не заважають один одному; оркестратор мержить їхню гілку. Плюс — жорсткий **file ownership** у межах фази.
- **Моделі агентів**: код — **fable**; ресьорч/аналіз/рев'ю/доки — **opus**. Якщо fable недоступний (квота) — код теж на **opus**. **sonnet НЕ юзати.**
- Контракти `src/bot2048/core/types.py` і `src/bot2048/recording.py` — незмінні без узгодження.
- Продакшн-якість: повні type hints, `mypy --strict`, ruff, pytest (+hypothesis для движка). У бібліотечному коді без `print` — тільки structlog; CLI-вивід — rich.
- Максимум телеметрії: кожна гра повністю пишеться в БД (`experiments → games → moves + spawns`).
- Порядок: симуляції локально → вибір найкращої стратегії за даними з БД → live на телефоні.

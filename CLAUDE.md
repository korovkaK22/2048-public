# monobank-2048 — бот для гри 2048 у застосунку monobank (Android)

Пайплайн: скріншот (ADB) → розпізнавання дошки по кольорах → expectimax → humanized свайп → повний запис у SQLite (кожен хід/спавн/рішення відтворюється постфактум).

## Документація (тримаємо чистою; нове — у профільний файл, не сюди)
- `docs/architecture.md` — **головний документ**: layout, контракти модулів, схема БД, фази. Читати першим.
- `docs/device-ui-notes.md` — усі нюанси UI телефона і гри (палітра, безпечні зони, баги застосунку). Нові спостереження про телефон/гру писати ТУДИ.
- `docs/live-setup.md` — підключення телефона, ADB, зняття скріншотів.
- `docs/rules-research.md` — відкриті питання про правила клону і як їх міряємо.
- `docs/ops-playbook.md` — операційні граблі: як правильно запускати Monitor, вбивати фонові процеси, ADB-по-WiFi, CLI/venv/тести. Читати перед живими прогонами.
- `docs/training-and-runs-census.md` — **зведення по всій роботі**: що натренували (архітектура, гіперпараметри, curriculum), скільки епізодів/прогонів, швидкості (eps/s, ходів/с), і таблиця «що зайшло / що закрили».
- `docs/knowledge/` — snapshot вивантаженої агентської пам'яті (2026-07-29): висновки про стелю скору, закриті глухі кути, операційні правила. Історичний дамп, не актуальна дока — див. `docs/knowledge/README.md`.

## Середовище
- Windows 10. Команди — через Bash (Git Bash); скрипти — `project/scripts/*.sh`, не `.ps1` (PowerShell-тула є, але скрипти репо мусять іти й на Linux/macOS).
- Python 3.12 (`py -3.12`); venv: `project/.venv` (створений, dev-залежності стоять).
- З `project/`: `./.venv/Scripts/pytest`, `./.venv/Scripts/ruff check src tests`, `./.venv/Scripts/mypy src`, CLI — `./.venv/Scripts/bot2048`.
- adb: `project/tools/platform-tools/adb.exe` (gitignored).
- Тимчасові файли (тестові скріншоти, чернетки, одноразові дампи) — у `tmp/` в КОРЕНІ репозиторію-папки (`C:\Users\sa095\Desktop\monobank-2048\tmp\`, поза git). Не смітити в `data/` чи `project/`.
- Docker — ТІЛЬКИ для батч-симуляцій; ADB/live — завжди на хості.

## Git та робочий процес
- **Репозиторій**: git у КОРЕНІ (`C:\Users\sa095\Desktop\monobank-2048`), `origin` → https://github.com/korovkaK22/monobank-2048.
- **Секрети НІКОЛИ не комітяться**: `.secrets/`, `*.env`, БД (`*.db`), бінарники тулів (adb/ffmpeg/scrcpy/`*.exe`/`*.dll`), raw-медіа — у `.gitignore`. Секрети лише з env; `.env.example` перелічує змінні (TG_BOT_TOKEN/TG_CHAT_ID/TG_USER_HANDLE у `.secrets/telegram.env`, MONO_PIN у `.secrets/device.env`).
- **Кожна фіча = окрема гілка + окремий коміт + пуш.** Не змішувати фічі в одному коміті.
- **Формат коміт-меседжа** (щоб з `git log` орієнтуватись без діфа): subject — коротко ЩО (imperative, ≤72 симв.). Body — кількома рядками **ЩО** зроблено, **ЯК** (підхід/ключова зміна) і **ЧОМУ** (яку проблему рішає, чому саме так). Приклади в історії: `feat: fast swipe … / FAST profile default, 80ms … / ~220-340ms/move saved`.
- **Лог експериментів**: кожен випробуваний варіант (евристика / набір ваг / depth / стратегія) → короткий запис у `docs/experiment-log.md` (config → результат → чому/висновок, найновіші зверху). Щоб не переганяти глухі кути й бачити всю історію того, що вже пробували і чим воно скінчилось.
- **Гілки**: `feat/…`, `fix/…`, `refactor/…`, `research/…`, `docs/…`, `chore/…`. У `main` — тільки мерж зеленої гілки (ruff + mypy + pytest).
- **Паралельна робота = git-worktree.** `project/scripts/new-worktree.sh <гілка>` → `worktrees/<гілка>/` (у `.gitignore`) з junction на спільний `project/.venv`. Прибирати ТІЛЬКИ `project/scripts/rm-worktree.sh <гілка>` (знімає junction першим; голий `git worktree remove --force`/`rm -rf` може пройти крізь junction і знести головний venv). У worktree CLI — `PYTHONPATH=src ./.venv/Scripts/python -m bot2048`, не `./.venv/Scripts/bot2048` (той editable з головної копії).
- **Зони (file ownership)** — одна паралельна задача = одна зона: `vision/` · `ai/` + `tools/engine2048|ntuple` · `device/` · `runner/` · `storage/` · `sim/` + `analysis/` · `ondevice/` + `ondevice-apk/` · `tgnotify/` · `docs/`. Зачіпаєш чужу зону — це окрема гілка або узгодження.
- **`docs/experiment-log.md`** мержиться як `merge=union` (`.gitattributes`) — записи з паралельних гілок не конфліктують; після мержу глянути, що блоки цілі.
- **Головна сесія = оркестратор**: декомпозує, запускає агентів, приймає результати, **мержить гілки в main і пушить** пофіче.
- **Агенти працюють у власних гілках/worktree** (isolation) — там створюють і пишуть, тому не заважають один одному; оркестратор мержить їхню гілку. Плюс — жорсткий **file ownership** у межах фази.
- **Моделі агентів**: код — **fable**; ресьорч/аналіз/рев'ю/доки — **opus**. Якщо fable недоступний (квота) — код теж на **opus**. **sonnet НЕ юзати.**
- Контракти `src/bot2048/core/types.py` і `src/bot2048/recording.py` — незмінні без узгодження.
- Продакшн-якість: повні type hints, `mypy --strict`, ruff, pytest (+hypothesis для движка). У бібліотечному коді без `print` — тільки structlog; CLI-вивід — rich.
- Максимум телеметрії: кожна гра повністю пишеться в БД (`experiments → games → moves + spawns`).
- Порядок: симуляції локально → вибір найкращої стратегії за даними з БД → live на телефоні.

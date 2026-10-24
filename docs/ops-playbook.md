# Ops playbook — дебаг і живі прогони

> Практичні операційні уроки для обох хостів — Mac mini (macOS, основний live) і
> Windows 10 / Git Bash; допоміжні скрипти — shell-скрипти в `scripts/*.sh`.
> Повний macOS-runbook (brew, launchd, живлення) — `macos-setup.md`.
> Це НЕ про правила гри (це в `device-ui-notes.md`) — це про те, як ПРАВИЛЬНО
> запускати інструменти й живі прогони, щоб не наступати на граблі двічі.

## Спостереження за живим прогоном (tail логів)
- **НЕ клади високочастотні debug-рядки у фільтр.** `adb.swipe` летить кожні ~2с —
  якщо він у grep, потік захлинається, і ти бачиш лише хвіст.
  Фільтруй ЛИШЕ значущі події.
- **Дві фази:** (1) під час активної фази — прогрес-віхи; (2) щоб зловити фінал —
  окремий tail ТІЛЬКИ на термінальні сигнали:
  `game_ended|end_reason|game.?over|supervisor.fatal|VISION_MISMATCH|MOVE_REJECTED|incident.bundle|not_in_game|auth_required`.
- **Покривай і провал, не лише успіх** — інакше краш виглядає як «тиша = все ок».
- Tail логів лише СПОВІЩАЄ, він не дає ВТРУТИТИСЬ. Щоб коректно зупинити/призупинити
  живий прогін на N-му ході — штатний `control/pause.flag`
  (раннер паркується між ходами; той самий прапорець ставлять команди
  `/pause` і `/resume` Telegram-моста), або вбити процес (див. нижче).

## Фонові процеси / вбивство дерева
### macOS (і Linux)
- **Коректний стоп раннера = `SIGTERM`**: `kill -TERM $(cat logs/runner.lock)` (lock = pid
  раннера). `runner/lifecycle.py` ловить SIGTERM/SIGHUP як Ctrl-C → `exit_reason='killed'`,
  heartbeat знято, вотчер НЕ підніме `WATCHDOG_TIMEOUT`. Під launchd —
  `launchctl kill TERM gui/$UID/com.bot2048.live`.
- Усе дерево (раннер + рушій `ntuple` + `adb`-діти) — по process group:
  `kill -TERM -$(ps -o pgid= -p <pid> | tr -d ' ')` (мінус перед pgid). Рушій і так помирає
  на EOF stdin, але група — страховка.
- Знайти: `pgrep -fl "bot2048.cli live"`, `pgrep -fl ntuple`; `launchctl print gui/$UID/com.bot2048.live`
  показує pid і last exit code.
- `kill -9` — лише коли завис (тоді `exit_reason` у `runs` лишиться NULL, і вотчер правдиво
  закричить).
- Закрив ssh-сесію з раннером у форграунді → SIGHUP → той самий чистий `killed`. Але краще
  запускати через launchd (`macos-setup.md` §6), а не з ssh.
- Сими поруч із live — під `taskpolicy -b` (E-ядра) і ≤ 4 воркери; CPU-affinity на macOS нема.

### Windows / Git Bash
- `taskkill` НЕ в PATH git-bash. Повний шлях + Windows-PID (WINPID = 4-й стовпець `ps -W`):
  `/c/Windows/System32/taskkill.exe //PID <WINPID> //T //F` (`//T` = все дерево).
- З git-bash дерево процесів вбиває саме `taskkill.exe` — не `kill` по MSYS-PID.
- `taskkill //F` — це TerminateProcess, НЕ сигнал: Python-обробник SIGTERM не спрацює,
  `exit_reason` лишиться NULL і вотчер підніме `WATCHDOG_TIMEOUT`. Коректний стоп на Windows —
  Ctrl-C у вікні раннера або `control/pause.flag`; перевіряй по `runs.exit_reason`.

### Спільне
- **SQLite: один писач.** Два `sim`/bench-прогони в одну `data/*.db` = каша/лок.
  Перед перезапуском бенчмарка вбий старе дерево процесів і витри `*.db *.db-wal *.db-shm`.
- Файл зайнятий (`Device or resource busy`) при `rm` = процес ще тримає БД; спершу вбий.

## CLI / venv
- Інтерпретатор venv: `.venv/bin/python` (macOS/Linux) або `.venv/Scripts/python.exe` (Windows);
  далі `$PY`. Лаунчери `scripts/live.sh`, `watch.sh`, `tg_send.sh`, `experiment.sh`, `bench.sh`
  знаходять його самі.
- Точка входу: `$PY -m bot2048.cli` (або `.venv/*/bot2048` — але в worktree це editable
  головної копії, тому там ТІЛЬКИ `-m bot2048.cli`).
  **НЕ** `python -m bot2048` — нема `__main__` (це пакет).
- Живий раннер потребує device-екстри: `$PY -m pip install -e ".[device]"`
  (adbutils; opencv/pillow уже стоять). Помилка «adbutils is not installed» = не поставлені екстри.
- Живий прод-запуск — через `scripts/live.sh` (env рушія: `w-l0.003.bin` + record-engine,
  `EVAL_MLP=0`/воркери на arm64, `MONO_PIN`, `caffeinate`), не голим CLI — інакше рушій
  обере мережу за вмістом `tools/ntuple/run/`.
- `vision parse|state <IMAGE>` — IMAGE позиційний, не `--image`.

## Тести / лінт
- Дублікати імен тест-файлів (напр. два `test_cli.py`) ламають збірку pytest у prepend-режимі.
  Фікс — унікальні імена (`test_vision_cli.py`), НЕ `--import-mode=importlib`
  (storage-тести роблять `from conftest import ...`, що потребує prepend).
- ruff: `RUF001/002/003` (ambiguous Cyrillic) і `UP040` винесені в `ignore` (україномовний
  проєкт + заморожений `core/types.py`). Не додавай нові `# noqa` замість цього.

## ADB через WiFi (звільняє USB, телефон на зарядці)
- Одноразово через USB: `adb -s <usb-serial> tcpip 5555`; далі `adb connect <ip>:5555`.
- Умови: та сама мережа, роутер без AP-isolation. Живе після від'єднання USB;
  злітає після ребута телефона / `kill-server` → перепідключити `adb connect <ip>:5555`.
- Закріпи статичний DHCP-lease, щоб IP не мінявся.
- **macOS 15 Local Network privacy**: `adb connect` з launchd-агента чи ssh може мовчки
  таймаутитись без дозволу в Privacy & Security → Local Network (запит спливає лише з
  GUI-терміналу). Перший тиждень на Mac mini — USB (`macos-setup.md` §4).
- Знімок екрана по WiFi ~0.65с/кадр (пляшкове горло живого циклу; scrcpy-стрім швидший).
- git-bash + adb shell зі шляхами: лапки або `MSYS_NO_PATHCONV=1` (інакше `/sdcard/..`
  переписується).

## Живі прогони (спостережувані факти по швидкості)
- Перевірений хід ≈ 2–3с на screencap (захват 0.65с × 2–4 кадри/хід + свайп+settle);
  ~0.43 ходу/с виміряно. Сліпі тапи — до ~19/с, але то не гра.
- `.secrets/` лежить у КОРЕНІ папки-репо (батько `project/`, ПОЗА git). tgnotify резолвить
  його walk-up'ом з пріоритетом теки з `.secrets/` над `.git`.
- Кожен інцидент зберігає `screenshot_before/after.png` — це найкраще джерело для доучування
  зору (нова плитка на екрані → калібруй колір/гліф з інцидент-кадру).

# macOS (Mac mini M4) як live-хост — runbook

Повний шлях від голого Mac mini до бота, що грає в monobank і шле алерти в Telegram,
плюс як запускати на ньому бектести. Windows-хост (i7) лишається підтримуваним, але
**основний live-хост — macOS**. Загальні речі про телефон і калібрування — у
[`live-setup.md`](live-setup.md); операційні граблі — у [`ops-playbook.md`](ops-playbook.md).

Залізо: Mac mini **M4, 4 performance + 6 efficiency ядер, 16 ГБ** (arm64). Усе, що
нижче позначено «тільки на Mac», з Windows-машини не перевіриш — це перший список
речей, які треба прогнати руками в першу годину (чекліст у кінці).

## 0. Що відрізняється від i7 (коротко)

| | Windows i7 | Mac mini M4 |
|---|---|---|
| adb / scrcpy / ffmpeg | `project/tools/*` (zip) | Homebrew (`/opt/homebrew/bin`), **один** adb на машині |
| venv | `.venv/Scripts/python.exe` | `.venv/bin/python` (brew `python@3.12`, не python.org — див. §8) |
| рушії Go | `*.exe`, `GOAMD64=v3` | нативний arm64, без `GOAMD64`; PGO-профіль перегенерувати ([`BUILD.md`](../project/tools/ntuple/BUILD.md)) |
| `BOT2048_EVAL_MLP` | за замовчуванням ON (виграш на x86) | **`0`** — на M4 регресія 8–15 % (`live.sh` ставить сам) |
| `BOT2048_SEARCH_WORKERS` | 8 (= NumCPU) | **4** = P-ядра (`sysctl -n hw.perflevel0.physicalcpu`; `live.sh` ставить сам) |
| не спати | — | `pmset`, `caffeinate -is` у `live.sh` |
| супервізія | ручний запуск у терміналі | launchd: `com.bot2048.live` (раннер), `com.bot2048.watch` (вотчер) |
| стоп процесу | `taskkill //PID //T //F` | `kill -TERM <pid>` → `exit_reason='killed'` (обробник SIGTERM/SIGHUP у `runner/lifecycle.py`) |
| чексуми рішень | еталон amd64 | **інші**: arm64 Go фьюзить FMA (див. §12) |

Конфіг прод-гри (**верифіковано з live-БД i7**; повне зведення — [`production-state.md`](production-state.md)):
чемпіон **`w-l0.003.bin`** + record-engine, expectimax **фіксованої глибини 4** (без
`--budget-ms`), `--prefetch`, `--capture apk`, `--auto-login` — це
`config/strategies/champion-live.json`. Фіксована глибина = гра не залежить від заліза
(той самий хід на M4 і на i7, з поправкою на FMA — §12); для Mac mini головні метрики —
пропускна здатність спроб і аптайм, не сила пошуку. Альтернативи:
`wall-record.json` (wall-мережа `champion-wall-snap-0316.bin`), `champion-l0003.json`
(record off). `scripts/live.sh` виставляє саме champion-live через env (тимчасово, доки
live-CLI не читає StrategySpec напряму).

## 1. Передумови

```bash
xcode-select --install                       # git, clang (brew сам попросить, якщо нема)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv)"    # і додати в ~/.zprofile
git clone git@github.com:korovkaK22/monobank-2048.git ~/monobank-2048
```

Секрети **не в git**: створити `~/monobank-2048/.secrets/` і скопіювати з Windows-машини
`device.env` (`MONO_PIN`) і `telegram.env` (`TG_BOT_TOKEN`, `TG_CHAT_ID`, `TG_USER_HANDLE`)
(`.env.example` у корені — перелік змінних). `chmod 700 .secrets && chmod 600 .secrets/*`.

## 2. Бутстрап хоста — `scripts/macos/setup.sh`

```bash
cd ~/monobank-2048/project
scripts/macos/setup.sh            # [--no-brew] [--no-weights] [--uv]
```

Що робить (ідемпотентно; кожен крок пропускається, якщо вже є):

1. `brew install --cask android-platform-tools` (adb), `brew install scrcpy ffmpeg go python@3.12`.
   **Не** запускати поруч `scripts/setup_adb.sh` з завантаженням у `tools/` — два adb
   воюють за сервер (на macOS з brew `setup_adb.sh` і сам іде через brew).
2. `project/.venv` на `python3.12` + `pip install -e ".[dev,device]"` (з `--uv` — через `uv`).
3. Обидва Go-рушії **нативно**: `go build -pgo=auto -o engine2048 .` і `-o ntuple .`
   (без `GOAMD64`). `default.pgo` у `.gitignore` і згенерований на x86 — без нього
   `-pgo=auto` просто no-op; перегенерувати на M4 за `BUILD.md`.
4. `scripts/fetch_weights.sh` — обидві мережі в `tools/ntuple/run/` (§3).
5. Друкує (не виконує) команди `pmset`, копіювання adb-ключа, перший запуск.

Ручна перевірка після: `adb version`, `scrcpy --version`, `ffmpeg -version | head -1`,
`go version` (має бути `darwin/arm64`), `.venv/bin/python -c "import cv2, adbutils"`.

## 3. Ваги

```bash
scripts/fetch_weights.sh
```

- `w-l0.003.bin` — **live-мережа** (чемпіон). Без закріпленої md5 — перевіряється як
  «бінарник ≥ 40 MB». Якщо релізу з нею нема, скрипт каже це явно і виходить з 1 —
  тоді скопіювати з Windows: `scp <win>:monobank-2048/project/tools/ntuple/run/w-l0.003.bin tools/ntuple/run/`.
- `champion-wall-snap-0316.bin` — wall-мережа (md5 `b030c966…`, 56 690 016 байт) для
  `wall-record.json`.

`live.sh` відмовиться стартувати без `run/w-l0.003.bin` (або файла з `BOT2048_NTUPLE_WEIGHTS`).
Ваги пінимо явно: безаргументний запуск `ntuple` обирає мережу за **наявністю файлів** у
`run/` (`serve.go weightCandidates`), а провенанс не має залежати від вмісту теки.

## 4. adb: ключ хоста, USB чи WiFi

**Ключ.** Телефон уже довіряє ключу Windows-машини. Щоб не підтверджувати «Allow USB
debugging?» (і не ловити `unauthorized` з launchd, де ніхто не натисне «Always allow»):

```bash
mkdir -p ~/.android
# з Windows: %USERPROFILE%\.android\adbkey і adbkey.pub  ->  ~/.android/
scp 'win:C:/Users/<user>/.android/adbkey*' ~/.android/
chmod 600 ~/.android/adbkey; chmod 644 ~/.android/adbkey.pub
adb kill-server; adb devices        # -> <serial>  device   (не unauthorized/offline)
```

**USB (рекомендовано для першого тижня).** Стабільно, не залежить від роутера, і не
зачіпає macOS-дозволів на локальну мережу. Телефон на зарядці від USB-порту Mac —
«Stay awake while charging» в Developer options.

**WiFi** (`adb tcpip 5555` раз через USB → `adb connect <ip>:5555`, статичний DHCP-lease):
⚠️ **macOS 15 Local Network privacy** — процес, що лізе в локальну мережу, має мати
дозвіл у System Settings → Privacy & Security → Local Network. З GUI-терміналу запит
спливе сам; **з launchd-агента чи ssh-сесії запиту не буде** — `adb connect` мовчки
таймаутиться. Якщо потрібен WiFi: спершу запустити `adb connect` з Terminal.app, дати дозвіл
Terminal/adb, і перевірити з `launchctl kickstart`; якщо не працює — USB. (Не перевірено
на живому Mac — audit A9.)

**APK-семплер** для `--capture apk` має бути встановлений і запущений на телефоні
(деталі — `device-ui-notes.md`): `adb shell pm list packages | grep bot2048` →
`ai.bot2048.sampler`. Збірка APK — `project/ondevice-apk/` (`./gradlew` тепер виконуваний
у git). Раннер сам робить `adb forward`.

## 5. Живлення і сон

```bash
sudo pmset -a sleep 0 disksleep 0 powernap 0 autorestart 1 displaysleep 10
pmset -g custom
```

- `sleep 0` — без idle-сну; `powernap 0` — без Power Nap (прокидання на мережу/бекапи
  рве adb); `autorestart 1` — повернутись після відключення світла.
- `live.sh` додатково тримає `caffeinate -is -w $$` рівно на час життя раннера (`-i`
  idle, `-s` system; **без** `-d` — екран може гаснути).
- System Settings → General → Software Update → автооновлення **вимкнути** (перезавантаження
  посеред партії).
- **FileVault + LaunchAgent**: після збою живлення агенти стартують лише після першого
  логіну. Варіанти: автологін без FileVault (рішення власника, audit A10), або просто
  знати, що після відключення світла треба зайти.

## 6. launchd — `scripts/macos/install.sh`

```bash
scripts/macos/install.sh          # рендерить шаблони -> ~/Library/LaunchAgents, bootstrap + enable
```

Два LaunchAgent'и ([`scripts/macos/README.md`](../project/scripts/macos/README.md)):

| агент | що | політика |
|---|---|---|
| `com.bot2048.watch` | `scripts/watch.sh` → `tgnotify watch` | `KeepAlive`, `ProcessType=Background`, стартує одразу |
| `com.bot2048.live` | `scripts/live.sh play --strategy go --depth 4 --prefetch --capture apk --auto-login --games 0` | `ProcessType=Interactive`, **без** `KeepAlive`, `RunAtLoad=false` — стартує людина |

```bash
launchctl kickstart -k gui/$UID/com.bot2048.live     # старт/рестарт раннера
launchctl kill TERM  gui/$UID/com.bot2048.live       # коректний стоп -> exit_reason='killed'
launchctl print      gui/$UID/com.bot2048.live | head -40      # state, pid, last exit
tail -f logs/launchd-live.err.log                    # stderr раннера (structlog JSON)
```

Чому раннер без `KeepAlive`: автоперезапуск після FATAL свайпав би по зламаному екрану;
єдиний дозволений авторестарт — бюджет у `runner/lifecycle.py` (6/год, cooldown 60 с).
Правити CLI-аргументи — у `com.bot2048.live.plist.template` → знову `install.sh`.

**Логи.** Застосунок пише `logs/*.jsonl` (`alerts.jsonl`, `heartbeat.json`, `tg_*.jsonl`),
launchd — `logs/launchd-{live,watch}.{out,err}.log`. Ротація через `newsyslog`
(`/etc/newsyslog.d/bot2048.conf`, sudo):

```
# logfile_name                                        [owner:group] mode count size when flags
/Users/<user>/monobank-2048/project/logs/launchd-live.err.log   <user>:staff 644 7 51200 * J
/Users/<user>/monobank-2048/project/logs/launchd-watch.err.log  <user>:staff 644 7 10240 * J
```

## 7. Процеси, пріоритети, сими поруч із live

- Стоп раннера з терміналу: `kill -TERM $(cat logs/runner.lock)` або всієї групи
  `kill -TERM -$(ps -o pgid= -p $(cat logs/runner.lock) | tr -d ' ')`. SIGTERM/SIGHUP
  обробляються як Ctrl-C → `exit_reason='killed'`, heartbeat знято, вотчер не кричить
  `WATCHDOG_TIMEOUT`. `kill -9` — тільки якщо завис (тоді `exit_reason` лишиться NULL).
- Рушій `ntuple` — дочірній процес раннера, помирає на EOF stdin; `AbandonProcessGroup=false`
  у plist — страховка.
- На macOS нема CPU-affinity. Раннер — `ProcessType=Interactive`; **усе інше** на цій
  машині під час live — `taskpolicy -c utility <cmd>` або `taskpolicy -b` (E-ядра).
- Бектести: `scripts/experiment.sh config/... --machine m4` (8 воркерів, GOMAXPROCS=1).
  **Поруч із live** — не більше 4 воркерів під `taskpolicy -b`, або взагалі не ганяти.
- Пам'ять: live-рушій ≈ 0.3–0.5 ГБ; 16 ГБ не обмеження, крім `EXACT_NODES` у десятки млн.

## 8. Telegram

brew-Python має нормальні CA-сертифікати; **python.org-збірка** дає
`CERTIFICATE_VERIFY_FAILED` в `urllib` (`tgnotify`) — не використовувати (audit A8).
Перевірка: `scripts/tg_send.sh "mac mini online"` → «відправлено». Вотчер —
`scripts/watch.sh` (або launchd `com.bot2048.watch`); `/status`, `/pause`, `/resume` з
Telegram працюють через `control/pause.flag` і `logs/` у `project/` (cwd-relative).

## 9. Де що лежить (cwd = `project/`)

`logs/` (алерти, heartbeat, lock, launchd-логи), `data/games.db` (повна телеметрія),
`control/pause.flag`, `tools/ntuple/{ntuple,run/*.bin}`, `tools/engine2048/engine2048`,
`../.secrets/` (корінь репо, поза git). Усі лаунчери самі `cd` у `project/`.

## 10. Перший запуск — чекліст (перша година на Mac mini)

1. `scripts/macos/setup.sh` → без помилок; `go version` = `darwin/arm64`.
2. `.secrets/` скопійовано, `~/.android/adbkey*` скопійовано, `adb devices` → `device`.
3. `adb shell pm list packages | grep bot2048` — семплер стоїть; `adb shell ps -A | grep sampler`.
4. `scripts/live.sh once --capture apk` — один кадр → стан → рішення; у виводі `version`
   рушія має бути `NTUPLE_WEIGHTS=…/w-l0.003.bin`, `BOT2048_RECORD_ENGINE=1`.
5. Одна партія в терміналі (не launchd): `scripts/live.sh --strategy go --depth 4 --prefetch --capture apk --auto-login --games 1`.
6. `.venv/bin/python -m bot2048.cli db latency --experiment live` — порівняти з i7 (§11).
7. `scripts/tg_send.sh "test"` → повідомлення прийшло.
8. `sudo pmset …` (§5), автооновлення off.
9. `scripts/macos/install.sh` → вотчер піднявся (`launchctl print gui/$UID/com.bot2048.watch`).
10. `launchctl kickstart -k gui/$UID/com.bot2048.live` → `tail -f logs/launchd-live.err.log`;
    через хвилину `launchctl kill TERM …` → у БД `exit_reason='killed'`
    (`sqlite3 data/games.db "select run_id, exit_reason from runs order by started_at desc limit 3"`).
11. Знову `kickstart` — і лишити грати; у Telegram має прийти `session_start`.

## 11. Як читати `db latency` і з чим порівнювати

```bash
.venv/bin/python -m bot2048.cli db latency --db data/games.db --experiment live
```

Таблиця «Move latency by phase — experiment live (N moves, capture=apk …)»:
рядки — фази `capture / perceive / detect / swipe / settle / search_ipc / search …`
плюс `TOTAL` і `CRITICAL`, колонки `p50 / p90 / p95 / max` мс; рядок з позначкою
`<== слабина` — фаза з найбільшим середнім внеском у критичний шлях. У підписі:
`critical path: avg X ms of Y ms full (Z%); prefetch hit-rate: H%`. Нижче — рядки
`predict-gate: hit …%` і `loop gap, unaccounted (gap - critical)` / `prev_commit (persist
section)` (p50/p95/p99/max): те, чого фазовий таймер не бачить між двома комітами ходу —
хвіст рекордера/логів; саме його мав прибрати async-рекордер, порівнювати до/після.

**Еталон Windows i7** (журнал `experiment-log.md`, 2026-07-11, 1731 хід, `--capture apk`):
**344/375 мс/хід** (критичний шлях / повний хід), свайп ≈ 141 мс (80 жест + ~60 Android
`input`-spawn), settle ≈ 156 мс, apk-gate 92 %, рушій ≈ 25 мс на критичному шляху. По всій
прод-історії i7 (`production-state.md`): **середнє 342 мс, медіана 312 мс на хід.**
На M4 чекаємо: `swipe`/`settle` ті самі (це телефон), `search`/`search_ipc` менші
(лист мережі ~9× швидший), `capture` той самий (APK-пакети). Якщо `TOTAL p50` помітно
вище 375 — дивитись `слабина`: `capture` → adb-транспорт (USB/WiFi), `swipe` → adb-latency,
`search` → `BOT2048_SEARCH_WORKERS`/`EVAL_MLP`.

## 12. Відомі відмінності рішень на arm64

Go на arm64 **фьюзить FMA** (`a*b+c` в одну інструкцію без проміжного округлення), amd64 —
ні. Тому `perfbench … -workers 1` на M4 друкує **інший `checksum=`**, ніж на i7, при тому
самому коді й мережі — це не баг. Еталон бітової ідентичності між збірками треба тримати
**в межах однієї архітектури** (два arm64-білди між собою; M4 ≡ M4 Pro). Для live це
означає: у майже-нічиїх позиціях окремі рішення на Mac можуть відрізнятися від i7;
статистика (медіана, хвіст) — та сама в межах шуму. Деталі — `BUILD.md`.

Інші навмисні відмінності: `BOT2048_EVAL_MLP=0` (ідентичні рішення, лише швидкість),
`BOT2048_SEARCH_WORKERS=4` (впливає тільки на budget-шлях; при фіксованій глибині 4 —
ні на що), PGO (лише швидкість).

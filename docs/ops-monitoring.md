# Ops & Monitoring — спостережуваність живих запусків

> Опис **реалізованої** підсистеми надійності живих прогонів. Дата першої версії: 2026-07-10.
> Код: `src/bot2048/runner/{alerts,escalation,heartbeat,incidents,lifecycle}.py`,
> `src/bot2048/storage/ops_schema.py`, `src/bot2048/device/supervisor.py`,
> вотчер — `src/bot2048/tgnotify.py` (`python -m bot2048.tgnotify watch`).
> Пов'язане: `docs/architecture.md`, `docs/device-ui-notes.md`, `vision/states.py` (ScreenState),
> `logging_setup.py` (structlog), `storage/schema.py` (SQLite). Заморожені контракти не зачіпаються —
> усе тут адитивне.

## TL;DR

- Бот пише **3 потоки**: debug/info → `logs/bot2048-YYYYMMDD.jsonl` (з ротацією за розміром);
  ключові події → SQLite (адитивні таблиці `runs`, `live_events`); **алерти WARN/ERROR/FATAL →
  `logs/alerts.jsonl`** (append-only, 1 JSON/рядок, UTF-8 без BOM, `\n`).
- Поруч із раннером стоїть вотчер `python -m bot2048.tgnotify watch`: читає `alerts.jsonl` за власним
  курсором і шле в Telegram кожен рядок з `level ∈ {ERROR, FATAL}`, а також синтезує
  `WATCHDOG_TIMEOUT`, якщо `logs/heartbeat.json` протух. Курсор-файл гарантує, що рядки, які прийшли,
  поки вотчер не працював, не губляться.
- Перед оголошенням ERROR раннер **сам верифікує скріншотом**: свіжий `screencap` (з ретраями 0-байт) +
  `detect_state`. Якщо стан `GAME` і дошка валідна — це WARN з авто-ретраєм; ескалація тільки якщо стан
  не-GAME або збій повторюється.
- Ескалація: **WARN** = авто-ретрай ботом → **ERROR** = 1 цикл безпечного відновлення Supervisor →
  **FATAL** = повний стоп (жодних тапів), алерт + incident-бандл, далі — ручне рішення.
- Кожен ERROR/FATAL має **incident-бандл** `logs/incidents/<ts>_<code>/` (скріншоти до/після, дошка,
  останні N ходів, traceback, проби класифікатора) — саме він і є вхідною точкою розбору інциденту.

---

## 1. Загальний потік

```
 runner (live loop)                          вотчер + оператор
 ───────────────────                         ─────────────────
 screenshot→parse→choose→swipe→record
   │ structlog INFO/DEBUG ──► logs/bot2048-YYYYMMDD.jsonl
   │ події партій/ходів   ──► data/games.db (games/moves/spawns + live_events)
   │ heartbeat кожні 10 с ──► logs/heartbeat.json (atomic replace)
   │
   ├─ збій → confirm_incident() (свіжий скріншот + detect_state)
   │     ├─ GAME + дошка ok → WARN → авто-ретрай (backoff)         [алерт WARN у alerts.jsonl]
   │     ├─ ретраї вичерпано / стан не-GAME → ERROR → Supervisor,   [алерт ERROR + бандл]
   │     │     1 цикл відновлення; не вийшло → FATAL
   │     └─ FATAL → СТОП (без тапів), алерт FATAL + бандл, exit(2)
   │
 logs/alerts.jsonl ◄──────────────────────── python -m bot2048.tgnotify watch
                                              │ ERROR/FATAL рядок або heartbeat протух
                                              ▼
                                             повідомлення в Telegram → оператор читає алерт
                                              + incident-бандл → усуває причину (код /
                                              ручні дії / логін у застосунку) → перезапускає раннер
```

## 2. Шари спостережуваності

### 2.1. Debug/info → `logs/bot2048-YYYYMMDD.jsonl`

- Структура задана `logging_setup.py` (structlog → JSONL, ISO-UTC timestamps, bound `game_id`/`ply`).
  Іменування за днем дає ротацію «за днем», плюс ліміт розміру: файл > 50 MB → `RotatingFileHandler`
  поверх денного імені переходить на суфікс `-1`, `-2`…
- Рівень для live-запусків: файл INFO (DEBUG вмикається прапорцем CLI).
- Нічого секретного: ПІН/токени **ніколи** не логуються (політика з `device-ui-notes.md`).

### 2.2. Ключові події → SQLite (адитивно до наявної схеми)

Наявні `experiments/games/moves/spawns` не зачіпаються — ходи/партії пишуться через `SqliteRecorder`.
Ops-шар додає дві таблиці (idempotent DDL у `storage/ops_schema.py`, поруч зі `SCHEMA_SQL`):

```sql
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,      -- run-20260710-140102-a1b2
    started_at  REAL NOT NULL,
    ended_at    REAL,
    pid         INTEGER NOT NULL,
    git_rev     TEXT,
    args_json   TEXT NOT NULL DEFAULT '{}',
    exit_reason TEXT                   -- 'clean' | 'fatal:<CODE>' | 'killed'
);

CREATE TABLE IF NOT EXISTS live_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    game_id      TEXT,                 -- FK на games.id, nullable (події поза партією)
    ply          INTEGER,
    level        TEXT NOT NULL,        -- INFO|WARN|ERROR|FATAL
    code         TEXT NOT NULL,        -- enum з §3 + 'HEARTBEAT_METRICS' для періодичних метрик
    message      TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    incident_dir TEXT                  -- відносний шлях, nullable
);
CREATE INDEX IF NOT EXISTS idx_live_events_run ON live_events(run_id, ts);
```

- У `live_events` йдуть: старт/кінець партій, рестарти, всі алерти (дубль alerts.jsonl — для SQL-аналітики),
  періодичні метрики (moves/min, avg eval_ms, screencap-retries, board_match_frac).
- Запис БД **не блокує** гру: той самий підхід, що в recorder (batch + flush на подіях);
  помилка запису в БД — це `STORAGE_ERROR` (WARN, гра триває, лог у файл).
- API: `OpsRepository.start_run/end_run/record_event`.

### 2.3. Алерти → `logs/alerts.jsonl`

- **Тільки** WARN/ERROR/FATAL. Append-only, один компактний JSON на рядок.
- Дисципліна writer'а (`runner/alerts.py::AlertWriter`) — критична для tail-вотчера:
  - `open(path, "a", encoding="utf-8", newline="\n")` — LF, UTF-8 **без BOM**;
  - `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` — один рядок, без вкладених переносів;
  - після кожного рядка `flush()` + `os.fsync()` — рядок або є цілком, або його нема;
  - бот ніколи не переписує файл; ротація — тільки вручну і тільки коли раннер зупинений (див. §7).

## 3. Контракт alert-рядка (схема v1)

```json
{
  "schema": 1,
  "ts": "2026-07-10T14:03:22.123456Z",
  "level": "FATAL",
  "code": "AUTH_REQUIRED",
  "message": "screencap 0 bytes x3; uiautomator anchor 'Уведіть ПІН-код' matched",
  "run_id": "run-20260710-140102-a1b2",
  "game_id": "b6b2f4c0-...",
  "move_no": 412,
  "screen_state": "auth_required",
  "board_hex": "0123002110450321",
  "attempt": 3,
  "incident_dir": "logs/incidents/20260710T140322Z_AUTH_REQUIRED",
  "extra": {"screencap_bytes": 0, "board_match_frac": 0.0}
}
```

Поля: `schema` (int, зараз 1), `ts` (ISO-8601 UTC, мікросекунди), `level` (`WARN|ERROR|FATAL`),
`code` (enum нижче), `message` (людський рядок), `run_id`, `game_id`/`move_no` (nullable),
`screen_state` (значення `ScreenState` або `null`, якщо кадр не знято), `board_hex` (16 hex, як у storage;
nullable), `attempt` (номер спроби в межах цього збою), `incident_dir` (nullable для WARN),
`extra` (довільний dict, малий).

**Enum `code`** (str-Enum `AlertCode` у `runner/alerts.py`):

| code | значення |
|---|---|
| `DEVICE_LOST` | adb не бачить пристрій / команда adb падає |
| `AUTH_REQUIRED` | вікно логіну банку (screencap 0 байт ×3 + uiautomator-анкор) |
| `VISION_MISMATCH` | розпізнана дошка суперечить очікуваній (після свайпу/спавну) |
| `MOVE_REJECTED` | свайп не змінив дошку, хоча хід був легальний |
| `GAME_OVER_UNEXPECTED` | `is_game_over` спрацював, хоча стратегія бачила ходи / розбіжність із попапом |
| `SCREEN_UNEXPECTED` | стійкий `NOT_IN_GAME` / `SCREEN_OFF` / `UNKNOWN` |
| `CRASH` | неперехоплений виняток раннера (top-level except) |
| `WATCHDOG_TIMEOUT` | синтезується вотчером: heartbeat протух (раннер завис) |
| `STORAGE_ERROR` | збій запису в SQLite (гра триває) |
| `RESTART_LOOP` | перевищено ліміт рестартів за годину |
| `LOOP_ERROR` | live-цикл відновився на місці після несподіваного винятку |

Правила еволюції: нові коди — можна; перейменування/зміна типу поля — тільки з bump'ом `schema`.
Вотчер фільтрує лише за `level`, тож нові коди його не ламають.

## 4. Incident-бандл

Тека `logs/incidents/<UTC-ts>_<code>/`, напр. `20260710T140322Z_VISION_MISMATCH/`:

```
alert.json            # копія алерт-рядка (pretty)
screenshot_before.png # останній «нормальний» кадр (раннер тримає його в пам'яті постійно)
screenshot_after.png  # свіжий кадр із confirm_incident(); відсутній якщо screencap 0 байт
uidump.xml            # uiautomator dump — ТІЛЬКИ якщо screencap порожній (кейс AUTH_REQUIRED)
board.json            # {"expected_hex": "...", "parsed_hex": "...", "board_match_frac": 0.93}
last_moves.json       # останні N=20 ходів: ply, direction, board_before/after, eval_ms (з пам'яті раннера)
state_probes.json     # StateDetection: state, reason, board_match_frac + значення анкорів (AnchorSpec hits)
traceback.txt         # для CRASH; інакше стек-знімок поточного місця (traceback.format_stack)
context.json          # run_id, game_id, move_no, args CLI, git_rev, версії пакетів
```

- Бандл створюється **до** запису ERROR/FATAL-алерту (щоб `incident_dir` в алерті вже існував).
- Для WARN бандл не створюється (тільки якщо WARN ескалує).
- Retention: бот не видаляє нічого; бандли, старші за 14 днів, чистяться вручну.

### Правило «перевір скріншотом, що екран нормальний» — `confirm_incident()`

Перед тим як підняти будь-який збій вище WARN, раннер робить таке:

1. Зробити **свіжий** `screencap` (політика 0-байт: до 3 ретраїв з паузою 0.3 с — як у `device-ui-notes.md`).
   Стабільно 0 байт ×3 → це `AUTH_REQUIRED` (FLAG_SECURE), підтвердити `uiautomator dump`-анкором.
2. Прогнати `detect_state()` + `parse_board()` на кадрі.
3. Рішення:
   - `state == GAME` **і** дошка валідна (`board_match_frac ≥ 0.9`, парситься без помилок) →
     це **WARN**: пере-синхронізуватися з розпізнаної дошки (не з кешу) і зробити авто-ретрай;
   - `state != GAME`, або дошка невалідна, або той самий `code` вже ≥3 разів за останні 10 хв →
     **ескалація** (ERROR, далі за §5).

## 5. Рівні ескалації: хто що робить

| Рівень | Хто діє | Дія |
|---|---|---|
| **WARN** | сам раннер | авто-ретрай на місці. Політика за замовчуванням: 3 спроби, backoff 0.5 / 1 / 2 с (screencap-ретраї — свої, 0.3 с). Алерт WARN пишеться, але вотчер його **не** пересилає. |
| **ERROR** | Supervisor FSM | рівно **1 цикл** безпечного відновлення: пере-класифікація стану → дозволені дії лише з таблиці нижче → повторна верифікація. Успіх → запис `recovered` у live_events, гра триває. Невдача → FATAL. |
| **FATAL** | оператор | раннер: СТОП негайно, **жодних тапів/свайпів**, FATAL-алерт + бандл, `runs.exit_reason='fatal:<CODE>'`, `exit(2)`. Вотчер шле повідомлення в Telegram; оператор читає бандл, усуває причину (фікс коду / логін у застосунку / ручні дії) і перезапускає раннер (§7). |

Пер-код політика — дані у коді (`runner/escalation.py::POLICIES`, dataclass `CodePolicy`:
`warn_retries`, `backoff_s`, `fatal_immediately`):

| code | WARN-ретраї | ERROR-відновлення Supervisor | коли одразу FATAL |
|---|---|---|---|
| `DEVICE_LOST` | 3× reconnect, backoff | `adb kill-server && adb start-server` + reconnect ×1 | пристрій не з'явився |
| `AUTH_REQUIRED` | — | — | **завжди одразу FATAL**: банк, пауза без тапів; авто-логін вимкнено за замовчуванням |
| `VISION_MISMATCH` | 3× пере-скріншот + пере-парс | resync дошки з екрана, скидання кешу очікувань | дошка невалідна повторно |
| `MOVE_REJECTED` | 2× повтор свайпу (з новим джитером) | 1 пере-класифікація (може висіти попап `CONFIRM_NEW_GAME`) | стан не-GAME після циклу |
| `GAME_OVER_UNEXPECTED` | 1× пере-парс | штатний flow рестарту партії («Нова гра» → «Так, нова гра» — калібровані тапи) | flow рестарту не спрацював |
| `SCREEN_UNEXPECTED` | 3× пере-скріншот (анімації) | `SCREEN_OFF` → wake (`input keyevent KEYCODE_WAKEUP`, це не тап у застосунку) | `NOT_IN_GAME`/`UNKNOWN` стійкі — тапи наосліп заборонені |
| `CRASH` | — | — | завжди FATAL |
| `STORAGE_ERROR` | 3× повтор транзакції | переоткриття connection | БД недоступна повторно → FATAL (дані партій втрачаються) |

Незмінний інваріант: **тапи — лише по каліброваних анкорах і лише у станах, де їх видно** (`CONFIRM_NEW_GAME`,
кнопка «Нова гра» в `GAME`). У будь-якому іншому стані Supervisor тапати не має права.

## 6. Watchdog / heartbeat

- Раннер кожні **N=10 с** (окремий daemon-thread `HeartbeatWriter`, незалежний від game-loop) атомарно
  перезаписує `logs/heartbeat.json`: write tmp → `os.replace`. Вміст:
  `{"ts": "...Z", "run_id": "...", "game_id": "...", "ply": 412, "pid": 12345}`.
- Watchdog живе **у вотчері** (окремий процес не потрібен): якщо mtime файла старший за
  **M=60 с**, вотчер синтезує алерт `WATCHDOG_TIMEOUT` і шле повідомлення. 60 с >> будь-якої
  легітимної паузи Humanizer'а; якщо з'являться навмисні довгі перерви (>45 с), heartbeat-потік
  усе одно тікає — тому false positive немає.
- На чистому виході раннер видаляє `heartbeat.json` (щоб вотчер не тригерився на зупинений вручну бот).

## 7. Рестарт-семантика («перезапустити код» безпечно)

- **Кожен запуск — новий `run_id`** (`run-<yyyymmdd-hhmmss>-<4hex>`), рядок у `runs`.
- Старт завжди «холодний» по відношенню до стану екрана: свіжий screencap → `detect_state` →
  **пере-розпізнати дошку з екрана; кешу/БД не довіряти** (застосунок міг перегенерувати партію — див.
  кейс перелогіну в `device-ui-notes.md`).
- Resume партії: незакрита гра в БД (`games.ended_at IS NULL`) закривається `end_reason='interrupted'`;
  відкривається **нова** `games`-рядок з `params_json: {"resumed_from": "<old_game_id>", "resume_board": "<hex>"}`.
  Ply нумерується з 0 заново; абсолютний рахунок екрана невідомий без OCR — фіксуємо це як відому
  властивість resumed-партій (аналітика фільтрує їх за `resumed_from`).
- Якщо пере-розпізнана дошка ≠ `resume_board` — це нормально (перегенерація): просто нова партія без resume.
- **Ідемпотентність**: рестарт безпечний у будь-який момент, бо кожне рішення починається зі свіжого
  скріншота; жодних відкладених тапів/черг; повторний запуск при вже запущеному раннері блокується
  lock-файлом `logs/runner.lock` (pid всередині; протухлий lock від мертвого pid — ігнорується).
- **Ліміти**: максимум **6 автоперезапусків за ковзну годину**, cooldown **≥60 с** між рестартами
  (лічильник — за `runs.started_at`, реалізація — `runner/lifecycle.py::enforce_restart_budget`,
  `RESTART_MAX_PER_HOUR=6`, `RESTART_COOLDOWN_S=60`). Перевищення → бот сам пише `RESTART_LOOP` FATAL
  і НЕ стартує; далі тільки ручне рішення.
- Порядок дій перед рестартом: прочитати алерт + бандл → усунути причину → запустити раннер →
  підняти вотчер поруч → за бажанням зробити ротацію: переіменувати
  `alerts.jsonl` → `alerts-<ts>.jsonl` і обнулити курсор (робити це можна ТІЛЬКИ коли раннер зупинений).

## 8. Вотчер алертів — `python -m bot2048.tgnotify watch`

Резервний шлях доставки: окремий процес, що читає `logs/alerts.jsonl` і `logs/heartbeat.json`
і шле ERROR/FATAL у Telegram (деталі моста — `docs/telegram-notify.md`). Потрібен саме як
резерв: внутрішній notify-тред раннера вмирає разом із раннером (kill, зависання, OOM), а
watchdog має спрацювати саме тоді.

Запуск (з кореня repo `monobank-2048`):

```
project\.venv\Scripts\python.exe -m bot2048.tgnotify watch --root .
```

Як він працює (`tgnotify.py::watch`):

- **Курсор — номер обробленого рядка** у `logs/tg_alerts.cursor` (алерт-файл малий, повне
  перечитування раз на 2 с дешевше і надійніше за byte-offset, який губить рядки між
  сесіями вотчера і плутається на ротації). Читання — UTF-8 явно.
- Недописаний/битий JSON-рядок пропускається — при наступному проході файл дочитується.
- Фільтр — **лише `level ∈ {ERROR, FATAL}`**; WARN не пересилається.
- **Grace 15 с**: алерт, молодший за 15 с, не відправляється — спершу шанс має внутрішній
  тред раннера. Далі — дедуплікація за журналом `logs/tg_sent.jsonl` (пара `(ts, code)`).
- **Фото** беруться з `incident_dir` алерта: `screenshot_after.png`, інакше `screenshot_before.png`.
- **Watchdog**: `logs/heartbeat.json` існує, але його mtime старший за 60 с → синтетичний
  `WATCHDOG_TIMEOUT` (одноразово, до відновлення heartbeat). Файла немає = чистий вихід,
  тривога не піднімається.
- Вотчер **не завершується** після події — він живе весь час живого прогону; у watch-режимі
  з вхідних команд доступні лише `/status` і `/help`.

Гарантія «не втратити рядки між сесіями»: курсор рухається лише після того, як рядок
*оброблено*, тож алерт, що прийшов, поки вотчер не працював, буде відданий першим при
наступному запуску. Наївний tail «лише нові рядки після старту» саме тому і не годиться.

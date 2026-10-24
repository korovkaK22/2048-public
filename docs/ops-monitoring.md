# Ops & Monitoring — спостережуваність живих запусків

> Опис **реалізованої** підсистеми надійності живих прогонів. Дата першої версії: 2026-07-10;
> формат бандла v2 / схема алерту v2 / єдиний `IncidentSink` — 2026-10-25 (`feat/incident-tracing`).
> Код: `src/bot2048/runner/{alerts,escalation,heartbeat,incidents,lifecycle}.py`,
> `src/bot2048/storage/ops_schema.py`, `src/bot2048/device/supervisor.py`,
> вотчер — `src/bot2048/tgnotify/watch.py` (`python -m bot2048.tgnotify watch`).
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
- Кожен ERROR/FATAL має **incident-бандл** `logs/incidents/<ts>_<code>/` (скріншоти до/після — «після»
  знято **свіжим** у момент інциденту, дошка, останні N ходів, traceback, проби класифікатора,
  контекст) — саме він і є вхідною точкою розбору інциденту. Усі термінальні збої йдуть **одним
  шляхом** — `runner/incidents.py::IncidentSink.record()` (§4), тож бандл, рядок `alerts.jsonl`
  (схема v2, §3), рядок `live_events` з `incident_dir` і Telegram-повідомлення з фото з'являються
  разом або не з'являються взагалі. Як пройти від повідомлення в Telegram до рядків у БД — §4.3.

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

## 3. Контракт alert-рядка (схема v2)

```json
{
  "schema": 2,
  "ts": "2026-07-10T14:03:22.123456Z",
  "level": "ERROR",
  "code": "MOVE_REJECTED",
  "message": "swipe UP rejected 5x in a row (board unchanged after every attempt) -- game b6b2... ply 412 ...",
  "run_id": "run-20260710-140102-a1b2",
  "game_id": "b6b2f4c0...",
  "move_no": 412,
  "screen_state": "game",
  "board_hex": "0123002110450321",
  "attempt": 5,
  "incident_dir": "logs/incidents/20260710T140322Z_MOVE_REJECTED",
  "extra": {"direction": "UP", "board_before": "0123...", "board_predicted": "1230..."},
  "attempts": 5,
  "retry_policy": {"max_attempts": 5, "retries": 4, "backoff_s": [0.25, 0.25, 0.25, 0.25],
                   "fatal_immediately": false, "source": "LiveConfig.swipe_retries", "note": "..."},
  "capture_kind": "probe",
  "stage": "swipe",
  "strategy": "go-expectimax(budget=120ms,farm)",
  "spec_hash": null,
  "engine": {"kind": "GoExpectimaxStrategy", "depth": 6, "budget_ms": 120.0, "farm": true,
             "limits": {"max_depth": 6, "budget_ms": 120.0, "max_nodes": null},
             "info": {"protocol": "2", "engine": "engine2048 ..."}}
}
```

Поля v1 (незмінні): `schema` (int), `ts` (ISO-8601 UTC, мікросекунди), `level` (`WARN|ERROR|FATAL`),
`code` (enum нижче), `message` (людський рядок — **причина**), `run_id`, `game_id`/`move_no` (nullable),
`screen_state` (значення `ScreenState` або `null`, якщо кадр не знято), `board_hex` (16 hex, як у storage;
nullable), `attempt` (номер спроби в межах цього збою), `incident_dir` (nullable лише для WARN; для
ERROR/FATAL **завжди** заповнений), `extra` (довільний dict, малий).

Поля v2 (адитивні, заповнює `IncidentSink`; у «голого» WARN — `null`):

| поле | що це |
|---|---|
| `attempts` | скільки спроб реально витрачено до цього алерту (напр. 5 свайпів, 3 перезняття, 20 re-poll'ів, 0 для AUTH без auto-login) |
| `retry_policy` | рядок таблиці ретраїв (§5) для цього коду як дані: `max_attempts`, `retries`, `backoff_s`, `fatal_immediately`, `source` (яка ручка конфігу це задає), `note`. Це **табличний дефолт**; реальне число — в `attempts` |
| `capture_kind` | звідки `screenshot_after.png`: `probe` (кадр проби, зроблений у момент збою), `fresh` (окремий свіжий `screencap` через хук), `blind_secure` (FLAG_SECURE, 0 байт — замість кадру `uidump.xml`), `device_lost` (adb мертвий, перезняття не робилось), `failed` (свіжий `screencap` впав — причина в `extra.capture_error`), `none` (хука немає) |
| `stage` | де в конвеєрі ходу зламалось: `capture` → `parse` → `verify` → `swipe`; поза ходом — `auth`, `restart`, `recovery`, `session`, `startup`, `record` |
| `strategy` | мітка стратегії (та сама, що в `games.strategy` / `GameStarted.strategy`) |
| `spec_hash` | хеш `StrategySpec`, якщо стратегія його несе (інакше `null`) |
| `engine` | `kind` (клас стратегії), `depth`/`budget_ms`/`farm`, `limits`, `info` (відповідь `version` go-движка, якщо вже піднятий) |

Повний зріз конфігу (ретраї, settle, auto_login, режим захоплення) і вся таблиця ретраїв — у
`context.json["session"]` бандла, не в рядку (рядок лишається компактним).

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

## 4. Incident-бандл (формат v2)

### 4.1. Один шлях для всіх термінальних збоїв — `IncidentSink.record()`

`runner/incidents.py::IncidentSink` — єдине місце, через яке проходить **кожен** збій, що
вичерпав свій бюджет ретраїв (§5): ERROR і FATAL Supervisor'а (`handle_error`/`fatal`),
`LOOP_ERROR` відновлення сесії (`SessionRecovery.recover`), `CRASH` guard'а (`lifecycle.run_session`),
`RESTART_LOOP` (і бюджет рестартів на старті, і churn коротких партій). Порядок дій усередині:

1. вибрати теку бандла (нова / **зчеплена** з попереднім ERROR / **дедуплікована**, див. 4.2);
2. зняти скріншоти: `screenshot_before.png` (останній здоровий кадр із `MoveRing`),
   `screenshot_last_parsed.png` (кадр, із якого парсили останню дошку) і `screenshot_after.png` —
   кадр **у момент інциденту**: кадр проби, якщо викликач щойно її зробив, інакше **свіжий
   `screencap`** через хук `MoveRing.set_capture()` (раннер ставить `device.screenshot` під спільним
   capture-lock, таймаут 5 с). Збій зняття **ніколи не ховає причину**: він записується в
   `context.json.capture_error` / `alert.extra.capture_error` і `capture_kind=failed`;
3. записати артефакти (best-effort пофайлово — один невдалий файл не зриває решту);
4. записати рядок у `alerts.jsonl` (схема v2) — звідси хук `AlertWriter.notifier` шле Telegram
   (`TgNotifier.notify_alert` → `sendPhoto` з підписом-причиною, фото — `tgnotify.incident_photo()`);
5. покласти копію `alert.json` у бандл і дзеркало в `live_events` з **`incident_dir` завжди
   заповненим** (збій БД → WARN `STORAGE_ERROR`, не стоп);
6. підчистити `logs/incidents/` (4.4).

Тека `logs/incidents/<UTC-ts>_<code>/`, напр. `20260710T140322Z_VISION_MISMATCH/`:

```
alert.json                  # копія алерт-рядка (pretty, схема v2) -- причина + stage + attempts
screenshot_before.png       # останній здоровий GAME-кадр (MoveRing.good_frame)
screenshot_last_parsed.png  # кадр, із якого парсили останню дошку (ObservationGate.last_frame)
screenshot_after.png        # кадр У МОМЕНТ інциденту (capture_kind: probe | fresh); нема -> див. capture_kind
uidump.xml                  # uiautomator dump -- ТІЛЬКИ якщо screencap 0 байт (AUTH / secure surface)
board.json                  # опційно: очікувана vs розпізнана дошка
last_moves.json             # останні N=20 ходів: ply, direction, board_before/after, eval_ms, score_total
state_probes.json           # проба: state, reason, board_match_frac, image_present, uidump_present
traceback.txt               # для CRASH/LOOP_ERROR -- реальний traceback; інакше стек-знімок місця виклику
context.json                # schema 2: run_id, game_id, move_no, stage, attempts, retry_policy,
                            #   capture_kind, capture_error, extra, session{strategy, spec_hash, engine,
                            #   capture_mode, config{swipe_retries, parse_retries, ...}, retry_table}
alert_fatal.json            # лише в ЗЧЕПЛЕНОМУ бандлі (4.2): FATAL, що прийшов після ERROR того ж коду
screenshot_fatal.png        #   ... його власний кадр у момент FATAL
state_probes_fatal.json     #   ... його проба
traceback_fatal.txt / context_fatal.json
alerts_repeat.jsonl         # повтори того ж коду в межах 30 с (дедуп, 4.2) -- по одному рядку-алерту
```

- Бандл створюється **до** запису ERROR/FATAL-алерту (щоб `incident_dir` в алерті вже існував, а фото
  для Telegram уже лежало на диску).
- Для WARN бандл не створюється (тільки якщо WARN ескалує).
- Де в бандлі що шукати при розборі: **причина** — `alert.json.message` (+ `stage`, `attempts`),
  **що бачив телефон** — `screenshot_after.png` (або `uidump.xml`, якщо екран захищений), **що бачив
  бот до цього** — `screenshot_before.png`/`screenshot_last_parsed.png` + `last_moves.json`, **чому
  нема кадру** — `capture_kind` + `capture_error`, **яка стратегія/движок грали** — `alert.json.engine`,
  `context.json.session`.

### 4.2. Зчеплення ERROR→FATAL і дедуплікація штормів

- **Зчеплення.** FATAL того ж коду, що прийшов протягом 120 с після ERROR (драбина «ERROR → один
  цикл відновлення → FATAL»), пишеться **в ту саму теку** як `alert_fatal.json` /
  `screenshot_fatal.png` / `state_probes_fatal.json` / `traceback_fatal.txt`. Один інцидент = одна
  тека; у старих i7-бандлах це були пари `…_SCREEN_UNEXPECTED` + `…_SCREEN_UNEXPECTED-2` з однаковою
  пробою — суфікс `-N` залишився лише для справжньої колізії «та сама секунда, той самий код».
  `incident_dir` обох алертів — спільний; у FATAL `extra.chained_from = "alert.json"`.
- **Дедуплікація.** Повтор того ж коду (будь-який рівень) у межах 30 с від **першого** алерту бандла
  не створює нової теки і не знімає нових скріншотів: один рядок дописується в
  `alerts_repeat.jsonl` попереднього бандла, `incident_dir` алерту вказує на нього,
  `extra.repeat_of` — його шлях. Так шторм `DEVICE_LOST` (ERROR, FATAL, LOOP_ERROR, ERROR, FATAL…
  щокілька секунд) коштує один бандл на вікно, але **кожен** рядок `alerts.jsonl`/`live_events` далі
  вказує на теку. Вікно рахується від першого алерту, тож тривалий шторм раз на 30 с таки отримує
  свіжий бандл зі свіжим кадром.
- Telegram-дедуп окремий і як був: `TgNotifier` глушить однакові `(level, code, text)` 60 с і дайджестить
  WARN (docs/telegram-notify.md).

### 4.3. Як трасувати: Telegram → бандл → БД

1. **Повідомлення в Telegram** (`💀 FATAL …` / `🔴 ERROR …` з фото) — текст = `alert.message`
   (причина), фото = кадр у момент інциденту з `incident_dir` (`screenshot_fatal.png` для зчепленого
   FATAL, інакше `screenshot_after.png`, далі `screenshot_last_parsed.png`, `screenshot_before.png`; на
   захищеному екрані фото немає — лише текст).
2. **Рядок алерту** — `logs/alerts.jsonl`, знайти за `code` + часом (Kyiv у повідомленні = UTC+3 від
   `ts`): `grep MOVE_REJECTED logs/alerts.jsonl | tail -1`. У рядку: `incident_dir`, `game_id`,
   `move_no`, `stage`, `attempts`, `retry_policy`, `capture_kind`.
3. **Бандл** — `incident_dir` з рядка (відносно кореня репо). Дивитись у порядку 4.1: `alert.json` →
   `screenshot_after.png` → `last_moves.json` → `context.json` → `traceback.txt`.
4. **БД** (`project/data/games.db`) — від `game_id`/`move_no`:

   ```sql
   -- усі події інциденту й сусідні рядки ops-журналу
   SELECT datetime(ts,'unixepoch'), level, code, ply, incident_dir, message
     FROM live_events WHERE game_id = :game_id ORDER BY ts;
   -- або від теки бандла назад до партії
   SELECT run_id, game_id, ply, code, level FROM live_events WHERE incident_dir = :incident_dir;
   -- останні ходи партії перед збоєм (board_hex тут і в last_moves.json збігаються)
   SELECT ply, direction, board_before, board_after, eval_ms FROM moves
     WHERE game_id = :game_id AND ply >= :move_no - 20 ORDER BY ply;
   -- як партія закрилась і чим (end_reason = vision_mismatch / move_rejected / device_lost / error ...)
   SELECT strategy, params_json, end_reason, final_score, num_moves FROM games WHERE id = :game_id;
   ```

   Рядок `MOVE_ABORTED` (INFO) теж несе `incident_dir` бандла, що зірвав хід, а `LOOP_ERROR` після
   FATAL — `incident_dir` того FATAL, тож від будь-якого рядка `live_events` з `incident_dir` один
   `SELECT` веде до теки й назад.

### 4.4. Retention і приватність

- `IncidentSink` після кожного **нового** бандла чистить `logs/incidents/` **від найстарішого**
  (за timestamp у назві) до меж **2 GiB / 1000 бандлів** (`SupervisorConfig.incidents_max_bytes` /
  `incidents_max_bundles`, константи `incidents.DEFAULT_MAX_TOTAL_BYTES` / `DEFAULT_MAX_BUNDLES`).
  Бандли **поточного запуску** (створені цим процесом або з timestamp ≥ старту сесії) не видаляються
  ніколи, навіть якщо бюджет перевищено. Ручна чистка «старше 14 днів» лишається доброю практикою.
- **Приватність.** Скріншоти — це екран **банківського застосунку**. Бандли живуть лише під `logs/`
  (у `.gitignore`, ніколи не комітяться); в Telegram фото йде **тільки** в приватний чат власника
  (`TG_CHAT_ID` із `.secrets/telegram.env`), ПІН у жоден артефакт не потрапляє (контракт
  `runner/auth_flow.py`). Не пересилати бандли далі й не класти їх у `data/`/репозиторій;
  архів старого хоста лежить у `project/data/live-history/` локально і теж не під git.

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

### 5.1. Таблиця ретраїв — єдине джерело

Пер-код політика — дані у коді (`runner/escalation.py::POLICIES`, dataclass `CodePolicy`:
`warn_retries`, `backoff_s`, `fatal_immediately`, `source`, `note`; `max_attempts = warn_retries + 1`).
Рядки, що мають ручку в конфігу, **читають її дефолт** (`LiveConfig.swipe_retries`,
`parse_retries`, `recover_max_per_hour`; `SupervisorConfig.transient_repolls` своєю чергою читає
`escalation.SCREEN_UNEXPECTED_REPOLLS`), тож таблиця й цикл не можуть розійтись мовчки; тест
`test_retry_table_mirrors_the_enforcing_knobs` це пінить. Конвенція за замовчуванням — **3 спроби**
(`escalation.DEFAULT_ATTEMPTS`), відхилення — тільки з причиною в колонці «чому». Саме цей рядок
(як `describe()`) потрапляє в `alert.json.retry_policy`; фактично витрачені спроби — в `attempts`.

| code | спроб до інциденту (`max_attempts`) | backoff | де задано (`source`) | чому саме так / що далі |
|---|---|---|---|---|
| `MOVE_REJECTED` | **5** (1 + `swipe_retries`=4) | 0.25 с ×4 (`swipe_retry_delay_s`) | `LiveConfig.swipe_retries` (CLI `--swipe-retries`) | жест інколи губиться при 45 мс свайпах; після 5-ї — ERROR + абoрт партії `move_rejected`. *Було в доці 2× — застаріле, у коді давно 4.* |
| `VISION_MISMATCH` | **3** (`parse_retries`) + повільні occlusion-ретраї 5× по 3 с + auto-learn | settle 0.13 с / 3 с | `LiveConfig.parse_retries`, `occlusion_max_retries` | анімація/лапа маскота; потім ERROR (`stage=verify`) + абoрт `vision_mismatch`. `stage=parse` — парсер кинув виняток: 1 цикл Supervisor, далі `LiveLoopError`→CRASH |
| `SCREEN_UNEXPECTED` | **21** (1 + 20 re-poll'ів × 0.5 с) | 0.5 с | `escalation.SCREEN_UNEXPECTED_REPOLLS` → `SupervisorConfig.transient_repolls` | ~10 с мають перекрити анімацію лапи маскота (інцидент 20260711T2040); `CONFIRM_NEW_GAME`/`SCREEN_OFF` — один калібрований крок (CONTINUE/wake). Стійкий не-GAME → FATAL, тапи наосліп заборонені. *Було в доці 3× — застаріле.* |
| `DEVICE_LOST` | **4** (1 + 3 спроби `screencap` в adb-шарі) | 0.5/1/2 с | `device.adb` + `SessionRecovery` | ERROR → FATAL одразу (нема безпечної дії) → відновлення сесії: reconnect + backoff 5 с, до `recover_max_per_hour`=6 на годину; `capture_kind=device_lost` (перезняття не робиться — adb мертвий) |
| `GAME_OVER_UNEXPECTED` | **2** (1 flow рестарту; після win — ще 6 сліпих re-poll'ів) | 0.8 с settle | `Supervisor.start_new_game` + `SessionRecovery` | FATAL, але transient: сесія відновлюється й пробує рестарт знову в межах `recover_max_per_hour` |
| `LOOP_ERROR` | **7** (6 відновлень на ковзну годину) | 5 с (`recover_backoff_s`) | `LiveConfig.recover_max_per_hour` | будь-який несподіваний виняток / transient FATAL; 7-й за годину — причина пропагує далі (CRASH/FATAL). *Було 3× — тепер мірить реальну ручку.* |
| `AUTH_REQUIRED` | **1** (0 ретраїв, `fatal_immediately`); з `--auto-login` — `auth_retries`=2 вводи ПІН | 3 с (`auth_settle_s`) | `LiveConfig.auth_retries` | банк: жодних тапів без opt-in; `capture_kind=blind_secure`, доказ — `uidump.xml` |
| `CRASH` | **1** | — | `lifecycle.run_session` | неперехоплений виняток → FATAL, exit(2), свіжий кадр через хук `MoveRing` |
| `RESTART_LOOP` | **1** | — | `lifecycle.enforce_restart_budget` (6/год, cooldown 60 с) / `LiveConfig.low_score_churn_limit`=8 | бюджет рестартів → FATAL без старту (бандл без скріншота — телефон ще не підключено, без рядка `live_events` — рядка `runs` ще нема); churn коротких партій → ERROR, сесія триває |
| `STORAGE_ERROR` | **1** (WARN, без бандла) | — | раннер | збій SQLite — WARN, гра триває; повтор транзакції не реалізований (`AsyncRecorder` сам чергує). *Було 3× — у коді ретраю нема, таблиця тепер чесна.* |
| `WATCHDOG_TIMEOUT` | — | — | `tgnotify.watch` | синтезує вотчер при протухлому heartbeat; раннер не піднімає |

Що саме робить Supervisor на рівні ERROR (один цикл, лише калібровані тапи):

| code | ERROR-відновлення Supervisor | коли FATAL |
|---|---|---|
| `DEVICE_LOST` | — (сліпі тапи заборонені) | одразу; далі `SessionRecovery` reconnect ×6/год |
| `AUTH_REQUIRED` | — | **завжди одразу FATAL**: банк, пауза без тапів; авто-логін вимкнено за замовчуванням |
| `VISION_MISMATCH` | свіжа проба; здоровий GAME → гра триває (партія вже абортнута з причиною) | екран не-GAME після циклу |
| `MOVE_REJECTED` | свіжа проба; може висіти попап `CONFIRM_NEW_GAME` → CONTINUE_GAME | стан не-GAME після циклу |
| `GAME_OVER_UNEXPECTED` | штатний flow рестарту («Нова гра» → «Так, нова гра» — калібровані тапи) | flow рестарту не спрацював |
| `SCREEN_UNEXPECTED` | `SCREEN_OFF` → wake (`KEYCODE_WAKEUP`, не тап у застосунку); `NOT_IN_GAME`/`UNKNOWN` → 20 re-poll'ів | стійкий не-GAME — тапи наосліп заборонені |
| `CRASH` | — | завжди FATAL |
| `STORAGE_ERROR` | — | ніколи (WARN) |

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

Як він працює (`tgnotify/watch.py::watch`):

- **Курсор — номер обробленого рядка** у `logs/tg_alerts.cursor` (алерт-файл малий, повне
  перечитування раз на 2 с дешевше і надійніше за byte-offset, який губить рядки між
  сесіями вотчера і плутається на ротації). Читання — UTF-8 явно.
- Недописаний/битий JSON-рядок пропускається — при наступному проході файл дочитується.
- Фільтр — **лише `level ∈ {ERROR, FATAL}`**; WARN не пересилається.
- **Grace 15 с**: алерт, молодший за 15 с, не відправляється — спершу шанс має внутрішній
  тред раннера. Далі — дедуплікація за журналом `logs/tg_sent.jsonl` (пара `(ts, code)`).
- **Фото** беруться з `incident_dir` алерта тим самим `tgnotify.incident_photo()`, що й у
  внутрішньому хуку раннера: для FATAL спершу `screenshot_fatal.png` (зчеплений бандл, §4.2), далі
  `screenshot_after.png` → `screenshot_last_parsed.png` → `screenshot_before.png`; нема жодного
  (захищений екран) — лише текст.
- **Watchdog**: `logs/heartbeat.json` існує, але його mtime старший за 60 с → синтетичний
  `WATCHDOG_TIMEOUT` (одноразово, до відновлення heartbeat). Файла немає = чистий вихід,
  тривога не піднімається.
- Вотчер **не завершується** після події — він живе весь час живого прогону; у watch-режимі
  з вхідних команд доступні лише `/status` і `/help`.

Гарантія «не втратити рядки між сесіями»: курсор рухається лише після того, як рядок
*оброблено*, тож алерт, що прийшов, поки вотчер не працював, буде відданий першим при
наступному запуску. Наївний tail «лише нові рядки після старту» саме тому і не годиться.

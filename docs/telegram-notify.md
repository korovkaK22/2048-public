# Telegram-міст — сповіщення та команди для користувача

> Опис **реалізованого** моста. Код: пакет `src/bot2048/tgnotify/` (до 2026-10 — один файл `tgnotify.py`). Дата першої версії: 2026-07-10.
> Пов'язане: `docs/ops-monitoring.md` (рівні, схема alerts.jsonl v1, heartbeat, incident-бандли),
> `docs/architecture.md`, `docs/edge-cases.md`. Заморожені контракти не зачіпаються — усе адитивне.
> Бот: `@your_bot`. Користувач: `@your_handle` (один; його chat_id = allowlist).
> Секрети: `.secrets/telegram.env` (`TG_BOT_TOKEN`, `TG_CHAT_ID`, `TG_USER_HANDLE`).
> **Токен ніколи не логувати, не комітити, не вставляти в доки/повідомлення/traceback.**

## TL;DR

- Пакет `project/src/bot2048/tgnotify/` (`notifier.py` — ядро, `transport/outbox/commands` — міксини, `watch/bootstrap/cli`): фоновий daemon-thread з чергою; API
  `notify(level, code, text, photo_path=None)` — fire-and-forget, **ніколи** не блокує і не валить раннер.
- Роутинг: FATAL/ERROR → завжди + фото з incident-бандла; WARN → тротлінг/дайджест; INFO-переходи
  станів → коротко. Сесійні: 🚫 «Бот працює — НЕ чіпай телефон» на старті, ✅ «телефон вільний» на стопі/краху.
- Вхідні команди тим самим тредом через `getUpdates`: `/status /screen /pause /resume /ack /help`;
  тільки від `TG_CHAT_ID`, решта ігнорується мовчки.
- Основний шлях доставки — **пряме API з раннера**; резервний — **standalone-вотчер**
  `python -m bot2048.tgnotify watch` (tail alerts.jsonl + перевірка heartbeat), бо якщо раннер помер,
  його внутрішній тред помер разом із ним.
- Тільки stdlib (`urllib.request`, `json`, `queue`, `threading`) — `requests` у pyproject відсутній.

## 1. Загальний потік

```
 runner (live loop)                                Telegram (@your_handle)
 ──────────────────                                ───────────────────
 старт сесії ── notify(INFO, SESSION_START) ─┐
 переходи/алерти ── notify(...) ─────────────┤   TgNotifier (daemon-thread)
 finally/atexit ── notify(INFO, SESSION_END) ┤    queue → sendMessage/sendPhoto
                                             └──► retry+backoff → logs/tg_outbox.jsonl (офлайн)
                                                  getUpdates (offset-курсор) ◄── /status /pause ...
                                                       │
                                                       └─► control/pause.flag, logs/tg_ack.flag

 резерв (раннер мертвий):
 logs/alerts.jsonl + logs/heartbeat.json ◄── python -m bot2048.tgnotify watch
                                              (курсор logs/tg_alerts.cursor, шле ERROR/FATAL/WATCHDOG)
```

Шляхи файлів — відносно кореня repo (`monobank-2048`), як у `ops-monitoring.md`.

## 2. Пакет `project/src/bot2048/tgnotify/`

### 2.1. Публічний API

```python
class TgNotifier:
    def start(self) -> None            # запускає 1 daemon-thread (worker)
    def notify(self, level: str, code: str, text: str, photo_path: str | None = None) -> None
    def session_start(self) -> None    # 🚫-повідомлення (див. §3.3)
    def session_end(self, ok: bool, reason: str = "") -> None   # ✅/💀-повідомлення
    def stop(self, flush_timeout: float = 5.0) -> None  # best-effort дослати чергу, не довше N с
tg = TgNotifier.from_env(".secrets/telegram.env")      # синглтон для раннера
```

- `notify()` лише кладе item у `queue.Queue(maxsize=200)`; переповнення → `put_nowait`,
  item мовчки скидається у outbox (§2.4). Жодних винятків назовні: тіло `notify` у `try/except Exception`.
- Конфіг `TgConfig(token, chat_id, user_handle)` — власний парсер `KEY=VALUE` (без python-dotenv);
  відсутній/битий env → notifier стартує в режимі no-op + один WARN у structlog (без значень секретів).
- Транспорт: `urllib.request.urlopen(req, timeout=5)`; `sendMessage` — POST
  `application/x-www-form-urlencoded`; `sendPhoto` — multipart/form-data (зібрати вручну, boundary =
  `uuid4().hex`). URL `https://api.telegram.org/bot<TOKEN>/<method>` тримати тільки локальною змінною;
  у логи писати лише `method`, `status`, `error class` — **ніколи повний URL**.

### 2.2. Worker-цикл (один тред на все)

```
loop:
  item = queue.get(timeout=0.2)      # вихідні — пріоритет
  якщо item: відправити (retry §2.3) → journal §6.2
  інакше, якщо минуло ≥2 с від останнього poll:
      getUpdates(offset=cursor+1, timeout=2, limit=10)   # короткий long-poll, §5
      обробити команди; cursor → logs/tg_updates.offset (atomic replace, як heartbeat)
  раз на 60 с: спробувати дослати logs/tg_outbox.jsonl (§2.4)
```

Затримки: доставка вихідного ≤ ~2.5 с, реакція на команду ≤ ~3 с — достатньо, і не треба
другого треда/локів. Тред `daemon=True`: зависання Telegram не тримає процес живим.

### 2.3. Помилки, retry, backoff

- Таймаут кожного HTTP-виклику **5 с**, retry ×3 з backoff 1/2/4 с (усередині worker, не в notify()).
- HTTP 429: прочитати `parameters.retry_after`, спати стільки (cap 30 с), повторити — це не «збій».
- Після 3 невдач → item у outbox, worker живе далі. Виняток будь-де в worker → `except Exception`:
  лог WARN у structlog (`TG_SEND_FAILED`, без токена), продовжити цикл. **Збій Telegram ніколи не
  ескалюється в алерт** (інакше цикл: алерт → відправка → збій → алерт).

### 2.4. Офлайн-outbox `logs/tg_outbox.jsonl`

- Append-only JSONL (та сама дисципліна writer'а, що в alerts.jsonl: UTF-8 без BOM, LF, flush+fsync):
  ```json
  {"ts":"2026-07-10T14:03:25Z","kind":"photo","level":"FATAL","code":"AUTH_REQUIRED",
   "text":"💀 FATAL ...","photo_path":"logs/incidents/20260710T140322Z_AUTH_REQUIRED/screenshot_before.png","attempt":3}
  ```
- Докачка: раз на 60 с worker читає outbox; успішно відправлені рядки позначає у
  `logs/tg_outbox.cursor` (номер рядка — та сама дисципліна курсора, що й у watch-режимі §6.1).
  Повне переписування файла — заборонено
  під час роботи; компакцію (видалити відправлене) робити лише на старті notifier'а.
- Старіші за 24 год повідомлення при докачці шлються одним дайджестом «⏳ поки не було мережі: …».

## 3. Роутинг за рівнями (`ops-monitoring.md` §3, §5)

### 3.1. Матриця

| рівень/подія | шлеться? | фото | примітка |
|---|---|---|---|
| FATAL | завжди, негайно | так: `screenshot_after.png` з `incident_dir`, fallback `screenshot_before.png`, нема — без фото | + «⚠️ ПОТРІБЕН ТИ» якщо code ∈ ACTION_REQUIRED |
| ERROR | завжди, негайно | так (те саме правило) | |
| WARN | тротлінг: перший — одразу, далі той самий `code` мовчки лічиться; дайджест раз на 10 хв «🟡 WARN ×7: VISION_MISMATCH ×5, MOVE_REJECTED ×2» | ні | |
| INFO-переходи | коротко, без тротлінгу | ні | старт партії, game over + рахунок, рестарт, resume |

`ACTION_REQUIRED = {"AUTH_REQUIRED", "RESTART_LOOP", "DEVICE_LOST(FATAL)", "WATCHDOG_TIMEOUT"}` —
константа-set у `tgnotify/formatting.py`; для них у тексті перший рядок: **«⚠️ ПОТРІБЕН ТИ»** + що саме зробити
(для `AUTH_REQUIRED`: «введи ПІН у застосунку і дай /resume»). Поки action_required не погашено
`/ack` — нагадування кожні 10 хв тим самим текстом (лічильник у пам'яті + `logs/tg_ack.flag`).

### 3.2. Точки виклику в раннері (адитивно)

- `runner/alerts.py::AlertWriter` — після успішного запису рядка в alerts.jsonl викликати
  `tg.notify(level, code, message, photo)` (фото — з `incident_dir`, якщо є).
- `runner/live.py` — переходи станів: `GAME_STARTED`, `GAME_OVER` (+ фінальний рахунок/таймер/max tile),
  `RESTARTED`, `RESUMED`, `RECOVERED` (успіх Supervisor).
- Не дублювати: якщо подія вже пішла через AlertWriter, вдруге з live.py не слати.

### 3.3. Сесійні повідомлення (вимога користувача)

- Старт live-раннера (одразу після захоплення `logs/runner.lock`):
  `🚫 Бот працює — НЕ чіпай телефон. run_id=run-20260710-140102-a1b2, стратегія expectimax d3.`
- Стоп — у **`try/finally` навколо головного циклу + `atexit`** (щоб покрити і чистий вихід, і
  FATAL-`exit(2)`, і KeyboardInterrupt; guard-прапорець від подвійної відправки):
  - чисто: `✅ Сесія завершена — телефон вільний. Партій: 3, кращий рахунок: 12480, час: 1г 12хв.`
  - крах/FATAL: `💀 Бот впав (CRASH) — телефон вільний, але подивись останнє повідомлення. ⚠️ ПОТРІБЕН ТИ` (якщо action_required).
- `stop(flush_timeout=5)` в тому ж finally: best-effort дочистити чергу; не встигли — усе в outbox,
  дошле watcher-режим або наступна сесія.
- Hard-kill процесу finally не покриє — це закриває резервний вотчер (§6): heartbeat протух →
  `WATCHDOG_TIMEOUT` → повідомлення «телефон вільний, але бот завис».

## 4. Формат повідомлень і ліміти

### 4.1. Шаблон

```
<емодзі> <LEVEL> <code>            емодзі: ℹ️ INFO · 🟡 WARN · 🔴 ERROR · 💀 FATAL · 🚫/✅ сесія
⚠️ ПОТРІБЕН ТИ — <що зробити>      (тільки якщо action_required)
<коротке людське пояснення українською, 1–2 рядки>
🕐 14:03 (Kyiv) · стан: GAME→AUTH_REQUIRED · рахунок: 8120 · таймер: 12:41 · хід: 412
```

Приклади:
- `ℹ️ Нова партія #4 (resume: ні). Поїхали.`
- `ℹ️ Game over: рахунок 12480, max 1024, 512 ходів, 18:22. Рестартую.`
- `🔴 ERROR MOVE_REJECTED — свайп не змінив дошку, Supervisor відновлює. 🕐 14:03 · хід 412` (+фото)
- `💀 FATAL AUTH_REQUIRED` / `⚠️ ПОТРІБЕН ТИ — введи ПІН у monobank, потім /resume` (+фото)

Час — локальний Києва (`zoneinfo("Europe/Kyiv")`), бо читає людина; в outbox/журналах — UTC ISO-8601.
Текст — plain text (без parse_mode: жодного екранування Markdown, ніщо не зламається від `_` у code).

### 4.2. Ліміти Telegram і черга

- **1 msg/s на чат**: worker тримає `last_sent_ts`, перед відправкою `sleep(max(0, 1.05 - Δ))`.
- **4096 символів** text / **1024** caption у sendPhoto: обрізати з `…` (traceback ніколи не слати
  цілком — перші 6 рядків, повний і так у incident-бандлі).
- **Дедуплікація**: ключ `(level, code, hash(text))`, вікно **60 с** — повтори мовчки дропаються з
  лічильником; лічильник додається до наступного дайджесту WARN.
- Черга maxsize=200; при переповненні INFO дропаються першими (перед put перевірити рівень).

## 5. Вхідні команди (getUpdates у worker-треді)

- `getUpdates(offset=<last+1>, timeout=2, limit=10, allowed_updates=["message"])`; offset-курсор —
  `logs/tg_updates.offset` (plain int, atomic replace) — після рестарту не переграємо старі команди.
- **Allowlist**: `message.chat.id == TG_CHAT_ID`, інакше update мовчки підтверджується offset'ом і
  ігнорується (без відповіді, без логу тексту — лише лічильник `ignored_updates`).
- Команди старіші за 5 хв (`message.date`) — ігнорувати (могли пролежати в outbox Telegram).

| команда | дія |
|---|---|
| `/status` | стан: живий/пауза, вік heartbeat, run_id, партія/хід/рахунок (з `logs/heartbeat.json` + останній INFO), останній алерт |
| `/screen` | свіжий `screencap` через device-шар (0-байт політика ×3; 0 байт → «екран захищено FLAG_SECURE — мабуть, банк просить ПІН») → sendPhoto |
| `/pause` | створити `control/pause.flag` (вміст: ts+who); відповідь «⏸ паузну після поточного ходу» |
| `/resume` | видалити `control/pause.flag`; відповідь «▶️ продовжую» |
| `/ack` | створити/оновити `logs/tg_ack.flag` — глушить 10-хв нагадування action_required |
| `/help` | список команд |

- **Контракт паузи**: раннер між ходами (перед screenshot) перевіряє `control/pause.flag`; є файл →
  спить по 2 с у циклі, heartbeat-тред тікає далі (щоб не зловити хибний `WATCHDOG_TIMEOUT`).
  На вході в паузу: `⏸ Пауза — можеш взяти телефон`; на виході: `🚫 Продовжую — не чіпай телефон`.
- `/screen` і `/status` виконуються прямо у worker-треді (adb-виклик ≤3 с з таймаутом); збій — чесна
  відповідь «не зміг зняти скрін: <клас помилки>».
- У watch-режимі (§6.1) доступні лише `/status` (heartbeat+останній алерт) і `/help`; решта відповідає
  «раннер не запущений».

## 6. Точки інтеграції: основний і резервний шлях

### 6.1. Рішення

- **Основний — пряме API з раннера** (§3.2): тільки так є фото, рахунок, сесійні 🚫/✅, INFO-переходи
  і мінімальна латентність. alerts.jsonl цього всього не містить (INFO туди взагалі не пишеться).
- **Резервний — standalone-вотчер** `python -m bot2048.tgnotify watch` (через `.venv`): той самий
  модуль, інший entrypoint. Потрібен, бо тред-нотифаєр умирає разом із раннером (kill, зависання, OOM).
  Він не завершується після події (деталі — `docs/ops-monitoring.md` §8):
  - tail `logs/alerts.jsonl` через **власний** курсор `logs/tg_alerts.cursor`;
    шле лише ERROR/FATAL;
  - heartbeat: `logs/heartbeat.json` старший за 60 с при живому файлі → синтетичний
    `WATCHDOG_TIMEOUT` → «💀 бот завис … ⚠️ ПОТРІБЕН ТИ» (одноразово до відновлення heartbeat);
  - запускається поруч із раннером; своя копія outbox-логіки.

### 6.2. Дедуплікація основний/резерв

- Основний шлях після успішної відправки алерта пише журнал `logs/tg_sent.jsonl`:
  `{"ts":"<ts алерта>","code":"...","sent_at":"..."}` (append-only, той самий writer).
- Вотчер перед відправкою: якщо `(ts, code)` є в журналі, або алерт молодший за **15 с** (grace —
  дати основному шляху відправити першим) — чекає; після grace без запису в журналі — шле сам.
  Гірший кейс подвійної доставки прийнятний; втрата — ні.

### 6.3. CLI-обгортка `scripts/live/tg_send.sh`

`scripts/live/tg_send.sh "текст" [шлях_до_фото]` — для ручних/скриптових відправок на будь-якій ОС:
- тонка bash-обгортка над `python -m bot2048.tgnotify send "text" [photo] --root .` (той самий
  `TgNotifier`/transport, що й у раннера: `.secrets/telegram.env`, таймаути, outbox); exit 0/1;
- токен лише в пам'яті `TgConfig`; у argv і в логах його нема; помилка друкується без URL.
- Колишній `tg_send.ps1` (PowerShell, `Invoke-RestMethod`) видалено — Windows-only і дублював
  транспорт.

## 7. Bootstrap `TG_CHAT_ID`

Одноразова процедура (якщо `TG_CHAT_ID` у env уже є — пропускається):

1. Користувач (`TG_USER_HANDLE=@your_handle`) відкриває `@your_bot` і тисне
   **/start** — Telegram не дає боту писати першим, без /start немає ані chat_id, ані доставки.
2. `python -m bot2048.tgnotify bootstrap`: цикл `getUpdates(timeout=25)` до 5 хв; знайти перший
   update, де `message.from.username == "your_handle"` → взяти `message.chat.id`.
3. Дописати/оновити рядок `TG_CHAT_ID=<id>` у `.secrets/telegram.env` (перезапис файла зі збереженням
   інших ключів; значення токена не друкувати). Відправити тест: «✅ Міст налаштовано».
4. Юзер так і не натиснув /start → після таймаута чіткий вивід: «немає updates — потрібен /start від
   @your_handle»; **не** брати chat_id від чужих юзерів (звірка username обов'язкова).
5. Порожній/відсутній `TG_CHAT_ID` у рантаймі → no-op режим + WARN у structlog «tg not bootstrapped».

## 8. Безпека секретів (інваріанти)

- `.secrets/` — у `.gitignore`; токен існує лише в пам'яті `TgConfig`.
- `TgConfig.__repr__` — маскує токен (`***`); URL з токеном не потрапляє в лог, traceback
  (`urllib.error.HTTPError` містить URL — він перехоплюється і перепаковується у власний виняток без URL),
  outbox, tg_sent.jsonl, повідомлення.
- Тест: греп усього `logs/` після інтеграційного прогону на підрядок токена — 0 збігів.

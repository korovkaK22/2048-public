# Он-девайс обробка: наскільки можна прискорити цикл «кадр → розпізнавання → рішення → свайп»

> Ресьорч, 2026-07-11. Джерела цифр: доки проєкту (`../live-setup.md`,
> `../ondevice-vision-design.md`, `../device-ui-notes.md`,
> `../ops-monitoring.md`), живі проби adb цього дня, веб-джерела (лінки внизу).
> Обмеження ресьорчу: без змін у `src/` і `tests/`; на телефон нічого не
> інсталювалось; файли іншого агента (`swipe_throughput.md`) не чіпались.

## 0. Базова лінія (поточний пайплайн, виміри проєкту)

| Компонент | Латентність | Джерело |
|---|---|---|
| `screencap -p` повний кадр (fallback) | ~0.65 с/кадр (0.64–0.90 с, PNG+WiFi) | live-setup.md |
| scrcpy H.264 stream (primary) | ~35–70 мс stream lag; декод 10–20 мс + семпл 16 клітинок ~26 мс | live-setup.md, ondevice-vision-design.md |
| adb shell spawn roundtrip (WiFi) | ~200 мс за доками; **жива проба сьогодні: 160–324 мс, медіана ~226 мс** (5× `adb shell echo`, 192.0.2.10:5555) | докa + проба |
| Свайп `input swipe` | фізичний жест ~60 мс мінімум; humanizer default 300 мс (`DEFAULT_SWIPE_DURATION_MS`) + adb-spawn зверху | humanizer.py, доки |
| Expectimax depth 3 (хост) | ~38–41 мс середнє, p95 ~120 мс | доки бенчмарків |

Отже перцепція (кадр→стан) коштує ~45 мс компуту + 35–70 мс stream lag, а ввід — сотні мс через adb-spawn + тривалість жесту.

## 1. Таблиця варіантів

| # | Варіант | Очікувана латентність «кадр→стан на ПК» | Складність (дні) | Ризики з monobank | Виграш проти поточного |
|---|---|---|---|---|---|
| 0 | *Поточний*: scrcpy full stream | ~35–70 мс lag + ~35–45 мс компут | 0 | вже працює; чорне під FLAG_SECURE на auth | — |
| 0.5 | scrcpy `--crop` на дошку+score | той самий lag, компут ~10 мс (декод 3–6 мс + семпл ~5 мс) | ~0.5–1 | як 0 | «ship now»: −30 мс компуту, але stream lag лишається |
| 1 | AccessibilityService APK (читання UI + `dispatchGesture`) | н/д — **дошка 2048 не видима в accessibility tree** (canvas/кастомний рендер; `uiautomator dump` у проєкті дає лише текстові анкори auth-екрана, не плитки) | 3–5 | **НАЙВИЩИЙ**: банківські апки сканують увімкнені accessibility-сервіси як tampering-сигнал; у дизайн-доці проєкту це **свідомо заборонено** («No AccessibilityService — deliberately avoided») | жести локально були б швидкі, але ризик логіна/бану неприйнятний → **dead end** |
| **2** | **MediaProjection APK: on-device класифікація → компактний стан на ПК** | **capture ≤16 мс (VSYNC) + класифікація 1–3 мс + 22 Б пакет <1 мс + декод на ПК <0.1 мс ⇒ ~20–30 мс «хід→пакет», ~1 мс роботи хоста** | 3–5 (прототип уже є: `src/bot2048/ondevice/` — Python-референс + golden vectors; Kotlin-скелет описано в дизайн-доці, «untested on-device») | 1 consent-tap MediaProjection; **без** accessibility/root/трафіку; auth-екран під FLAG_SECURE → чорні кадри (детектиться, це фіча) | прибирає stream lag і ~200 мс adb-spawn з перцепції; хост читає 22 Б замість відеопотоку |
| 3 | Повністю он-девайс бот (рішення теж на телефоні: gomobile bitboard / C++ NDK) | як 2 мінус мережевий hop (~1–2 мс LAN) | +2–4 поверх 2 | як 2 | **мізерний**: рішення на ПК коштує 38–41 мс і LAN-hop ~1–2 мс; переносити engine = вигода ≤2 мс, зате втрачаємо гнучкість тюнінгу ваг/глибини. CPU телефона depth 4–6 потягне повільніше за десктоп → не варто |
| 4a | uiautomator2 (py-клієнт + atx-agent на девайсі) | жести ~50 мс через постійний HTTP-сервер | 1 | **встановлює сторонній agent-APK** (atx-agent) — зайвий процес на девайсі поруч із банком; проба: зараз НЕ встановлений (`pm list packages` чистий) | лише ввід; перцепцію не чіпає |
| 4b | scrcpy control socket (інжекція жестів БЕЗ нового APK) | інжекція одиниці-десятки мс по вже відкритому сокету (scrcpy-server уже пушиться проєктом) | ~1 | мінімальний — та сама scrcpy-поверхня, що вже використовується | прибирає adb-spawn з вводу; **це доріжка агента swipe_throughput — тут лише згадка як комплемент** |
| 4c | Shizuku | дає API-рівень shell без root, але сам ставиться як APK + активація через adb; жестам майже нічого не додає понад 4b | 1–2 | зайвий привілейований сервіс на девайсі | не виправданий для нашого кейсу |
| 4d | Termux + python на телефоні | інтерпретатор повільний; capture однаково потребує MediaProjection/screencap | 2–3 | сторонній додаток + фонові процеси | гірший за 2 в усьому |

### Ризики monobank (звідні факти з доків проєкту)
- **FLAG_SECURE**: екран логіну — під FLAG_SECURE (`screencap` = 0 байт ×3 — це канонічний AUTH-оракул проєкту); **ігрове вікно 2048 — НЕ secure** (screencap/stream працюють у грі). MediaProjection на secure-вікні дає чорний кадр — тобто APK бачитиме auth так само, як ми зараз (нуль інформації, але надійний сигнал стану).
- **Accessibility** — детект-вектор №1 у банківських апок; у проєкті заборонено явно.
- **MediaProjection** — 1 consent-tap, системний прошений API, не чіпає процес банку, без overlay; у дизайн-доці визнано прийнятним. APK має бути `exported=false`, без storage, без мережевих портів назовні (тільки `adb forward` localabstract).

## 2. Рекомендація

**Варіант 2 — MediaProjection APK, on-device класифікація плиток, рішення на ПК**
(= option D дизайн-доки; підтверджено пріоритетом користувача).

- Перцепція «зміна на екрані → стан у боті на ПК» падає з ~70–115 мс до **~20–45 мс**, робота хоста — до ~1 мс.
- Після цього **вузьким місцем стає фізичний свайп** (~60–300 мс) — його оптимізує паралельний трек (scrcpy control socket, див. 4b і swipe_throughput.md іншого агента).
- Найкраще співвідношення виграш/зусилля, бо ~60% роботи вже зроблено: `packet.py` (wire-формат + golden vectors), Python-референс класифікатора, `assets/calibration.json` (центри клітинок + палітра), задокументовані підводні камені Kotlin-порту (`ImageReader rowStride`, median-семпл).
- Проміжний крок «ship now» без APK: **варіант 0.5 (`scrcpy --crop`)** — ~1 день, −30 мс компуту вже сьогодні, поки APK доробляється.

## 3. План впровадження варіанта 2 (деталізований)

### 3.1 Компоненти

```
[Телефон]                                   [ПК]
MediaProjection → VirtualDisplay            PacketSource (FrameSource-sibling,
  → ImageReader (RGBA, 1 buffer)              читає 127.0.0.1:<port>)
  → CellSampler (16 центрів з                 → BoardPacket.decode (<0.1 мс)
    calibration.json, median 3×3)             → існуючий state machine
  → TileClassifier (палітра, nearest          → expectimax на ПК → хід
    color, поріг впевненості)               ← SYNC_REQ / хід — окремим каналом
  → PacketWriter (localabstract socket)       (свайпи — поза скоупом, інший агент)
Foreground service (mediaProjection type,
Android 14+ manifest-вимога), exported=false
```

### 3.2 Протокол ПК↔APK

**Транспорт:** `adb forward tcp:<port> localabstract:bot2048` — постійний сокет
через adb-тунель (USB або WiFi-adb). Ніяких відкритих LAN-портів на телефоні
(безпечніше і працює однаково для USB/WiFi). Прямий WiFi TCP — лише як
опційний fallback, якщо adb-тунель виявиться нестабільним.

**Кадр даних device→host:** існуючий фіксований **22-байтний `BoardPacket`**
(уже специфікований і покритий golden vectors у `src/bot2048/ondevice/`):

| Поле | Розмір | Зміст |
|---|---|---|
| magic | 2 Б | `B2` — resync-by-magic при розсинхроні потоку |
| version | 1 Б | версія формату |
| flags | 1 Б | `BOARD_CHANGED`, `SECURE_SUSPECTED`, + нові: `NOT_IN_GAME`, `FRAME_FOLLOWS` |
| seq | u16 | лічильник — детект втрат/перестановок |
| board | 8 Б | 16 nibbles (log2 плитки, 0=порожньо) |
| uncertain | u16 | бітмаска клітинок з низькою впевненістю класифікації |
| device_ts_ms | u32 | таймстемп девайса — вимір наскрізної латентності |
| CRC-16/CCITT-FALSE | 2 Б | known-vector `0x29B1` у тестах |

Score в пакеті **не потрібен**: рушій на ПК веде рахунок сам по ходах; OCR
score — контрольна перевірка через full-frame sync (див. 3.4).

**Кадри host→device (control frames, у дизайн-доці «defined, not yet built»):**
маленькі TLV-повідомлення тим самим сокетом: `SYNC_REQ` (запросити повний
скріншот), `PING` (health), `SET_RATE` (heartbeat-період). Свайп-команди сюди
НЕ кладемо, поки триває окремий бенчмарк вводу.

### 3.3 Частота відправки станів

- MediaProjection віддає кадри на VSYNC (~60 Гц); класифікація 16 клітинок
  ~1–3 мс — тягнемо кожен кадр, **дедуплікуємо на девайсі** (MediaProjection
  редоставляє ідентичні кадри на refresh rate — відкидати дублі до класифікації,
  порівнянням хеша семпльованих пікселів).
- **Пакет шлемо тільки коли стан дошки змінився** (`BOARD_CHANGED`) — під час
  анімації зливання буде 2–4 проміжні пакети, host-side state machine уже вміє
  чекати стабілізацію.
- **Heartbeat**: якщо змін нема — порожній пакет кожні 500 мс (host детектить
  смерть APK по тиші > 1.5 с і падає на існуючу драбину fallback:
  `StreamingCaptureDevice` → scrcpy → screencap).

### 3.4 Full-frame sync раз на ~10 с

Мета: перевірка, що локальний парсер не з'їхав; детект попапів/несподіванок;
OCR score/timer drift-check; авто-донавчання палітри (`vision.parser.learn_tile_color`).

Механізм — **pull-based**: хост раз на 10 с шле `SYNC_REQ` → APK відповідає
повідомленням `FRAME` (flags: `FRAME_FOLLOWS`, далі u32 довжина + JPEG q≈80
поточного кадру, ~100–300 КБ, по LAN/adb-тунелю ~20–50 мс). Pull, а не push,
щоб хост керував смугою і не отримував кадр посеред критичної секції.

Додатково APK **сам** ініціює `FRAME` без запиту, коли:
- `uncertain`-маска ≥ N клітинок (класифікатор «не впевнений» → хай хост гляне);
- виставлено `NOT_IN_GAME` або `SECURE_SUSPECTED` (перехід стану).

Хост на отриманому кадрі жене той самий повний vision-парсер, порівнює з
останнім пакетом; розбіжність → `VISION_MISMATCH`-подія: пауза рушія,
recalibrate/relearn палітри, у крайньому разі fallback на scrcpy-стрим.

### 3.5 Детект «не-ігрових» екранів локально

Дешеві перевірки на девайсі поверх тих самих семплів:

1. **Чорний кадр** (усі 16 клітинок + 4 sentinel-пікселі поза дошкою ≈ чорні) →
   `SECURE_SUSPECTED`: це auth/логін під FLAG_SECURE. Хост підтверджує
   канонічним оракулом (`screencap` 0 байт ×3 + uiautomator-анкори
   «Уведіть ПІН-код») і жене існуючий авто-логін. APK в цей час нічого не бачить —
   і не мусить.
2. **Попап поверх гри** (game-over, «нова гра», системні діалоги): плитки
   перекриті/затемнені → багато клітинок поза палітрою → `uncertain`-маска
   масивна; якщо ≥8 клітинок непевні N кадрів поспіль → `NOT_IN_GAME` +
   авто-`FRAME`. Рішення що робити (тап по кнопці, `CONFIRM_NEW_GAME`-флоу)
   лишається на хості по повному кадру — попапи рідкісні, латентність некритична.
3. **Sentinel-пікселі** (2–4 точки стабільного хрому апки поза дошкою, з
   calibration.json): якщо і дошка непевна, і sentinel-и не збігаються — ми не
   в `Game2028Activity` взагалі → `NOT_IN_GAME` негайно (1 кадр, без очікування).

У всіх трьох випадках APK **продовжує слати пакети** (з флагами, board може бути
сміттям — хост ігнорує board при `NOT_IN_GAME`/`SECURE_SUSPECTED`), щоб канал
жив і хост бачив момент повернення у гру (`BOARD_CHANGED` + чиста маска).

### 3.6 Етапи і оцінка

| Крок | Обсяг | Оцінка |
|---|---|---|
| 0. `scrcpy --crop` proxy (варіант 0.5) — виграш уже зараз | host-only | 0.5–1 д |
| 1. Kotlin APK: MediaProjection + ImageReader + порт CellSampler/TileClassifier (спека і golden vectors готові) | APK | 1.5–2 д |
| 2. Socket-сервер (localabstract) + PacketWriter; host-side `PacketSource` + фолбек-драбина | обидва | 1 д |
| 3. Control frames: `SYNC_REQ`/`FRAME`/heartbeat; `VISION_MISMATCH`-петля на хості | обидва | 1 д |
| 4. Локальний детект не-ігрових станів (флаги, sentinel-и) + наскрізний прогін на живому девайсі, звірка `device_ts_ms` | обидва | 0.5–1 д |
| **Разом** | | **~4–6 д** |

Підводні камені (з дизайн-доки): `ImageReader` `rowStride ≠ width*4` (падінг
рядків); брати median 3×3, не один піксель; consent-діалог MediaProjection
після ребута — потрібен повторний тап (тому варіант E, app_process jar,
залишений як fallback для повністю unattended-сесій); Android 14+ вимагає
foreground service type `mediaProjection`.

## 4. Веб-джерела

- MediaProjection: [офіційний гайд](https://developer.android.com/media/grow/media-projection), [API reference](https://developer.android.com/reference/android/media/projection/MediaProjection) — VirtualDisplay+ImageReader, foreground-service вимоги.
- Практика низьколатентного capture: [LSFG-Android](https://github.com/FrankBarretta/LSFG-Android/releases) — дедуп кадрів (до 75% дублів на 120 Гц), уникати CPU memcpy, shallow ImageReader queue.
- scrcpy control-протокол (для треку вводу): [develop.md](https://raw.githubusercontent.com/Genymobile/scrcpy/master/doc/develop.md), [Tango ADB touch event](https://tangoadb.dev/scrcpy/control/touch/), [py-scrcpy-client control](https://leng-yue.github.io/py-scrcpy-client/_modules/scrcpy/control.html), [інжекція touch, issue #5473](https://github.com/Genymobile/scrcpy/issues/5473) — постійний сокет замість adb-spawn ⇒ одиниці-десятки мс на жест; нюанс Android 13 ([issue #3186](https://github.com/Genymobile/scrcpy/issues/3186), пофікшено).

## 5. Живі проби цього ресьорчу (без інсталяцій)

- `adb shell echo` roundtrip, WiFi 192.0.2.10:5555, 5×: **160/212/226/262/324 мс** — підтверджує репо-цифру ~200 мс adb-spawn overhead, який пакетний сокет прибирає повністю.
- `pm list packages`: atx-agent / uiautomator2 / termux / shizuku **не встановлені** — проби їхніх жестів без інсталяції неможливі, оцінки в таблиці з документації.

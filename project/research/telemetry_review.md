# Аудит телеметрії monobank-2048 — покриття, прогалини, що додати першим

Дата: 2026-07-11. Статус: **аудит покриття телеметрії** — зріз на дату аудиту, не імплементація.
Частину «топ-5 додати першими» згодом реалізовано (M1 vision-fallback-лічильник, M2 predict-gate,
M3 лічильники кадрів/спроб + фактичний `capture_kind`, N6 per-game агрегати в `GAME_OVER.details_json`):
`runner/move_telemetry.py` (`_MoveCounters`, `_GameTelemetry`), `runner/live.py`, читач —
`bot2048 db latency` (`storage/latency.py`). Тож колонки «є / бракує» нижче місцями застаріли.
Мета: «кращі логи» під **живий пайплайн**, **debug shakedown** і **50k-кампанію** (30–50 ігор без нагляду),
щоб кожна гра відтворювалась постфактум і щоб ігри кампанії можна було порівнювати по даних з БД.

Правило всіх пропозицій нижче: **additive**, без зміни заморожених контрактів
(`recording.py`, `storage/schema.py`, `sqlite_recorder.py`). Три легальні місця запису:
- **A.** `moves.eval_details_json` → неймспейс `"timing"` (вже так пише live-раннер) або поруч у тому ж dict;
- **B.** ops `live_events.details_json` (вільний JSON, `OpsRepository.record_event(..., details=...)`);
- **C.** тільки structlog (для живого потоку логів, не для агрегатів).
Де потрібна нова **колонка** чи поле `MovePlayed` — позначено **[КОНТРАКТНА ЗМІНА]** (Tier B з `latency_telemetry.md`).

---

## 1. Що вже логуємо (мапа джерел)

| Зона | Канал | Що є |
|------|-------|------|
| Per-move (committed) | `moves` рядок | `direction`, `board_before/after`, `score_*`, `eval_ms`, `search_depth`, `eval_details_json` |
| Per-move search | `eval_details` | `engine_ms`, `prefetch_hit`, `eval` (nodes), стратегія-специфіка (з `decision.details`) |
| Per-move timing | `eval_details["timing"]` | `capture_ms, perceive_ms, detect_ms, swipe_ms, settle_ms, total_ms` (5 фаз, реалізовано; `_MovePhaseTimer`) |
| Spawn | `spawns` рядок | `cell`, `exponent`, `board_after` (кожен хід + стартові плитки ply −1/−2) |
| Per-game | `games` рядок | `final_score`, `max_exponent`, `num_moves`, `end_reason`, `duration_s`, `started_at/ended_at` |
| Per-game підсумок | ops `GAME_OVER.details_json` | `tile_histogram`, `highest`, `empty`, `tile_sum`, `end_reason` |
| Інциденти/алерти | `alerts.jsonl` + ops `live_events` (дубль) | WARN/ERROR/FATAL: code, `screen_state`, `board_hex`, `attempt`, `incident_dir`, `extra` |
| Ops-події INFO | ops `live_events` | `GAME_STARTED`, `GAME_OVER`, `PAUSED/RESUMED`, `recovered`, `RESUMED(auth)` |
| Ран | `runs` | `pid`, `git_rev`, `args_json`, `exit_reason`, `started/ended_at` |
| Latency-читач | `db latency` CLI (`storage/latency.py`) | p50/p90/p95/max по 5 фазах + search-розріз по `prefetch_hit`, verdict «слабина» |
| Постмортем-кадр | `logs/game-screens/<id>_<reason>.png` | останній кадр кожної фіналізованої гри |
| Vision (per-cell) | structlog | `color_fallback`, `unknown_tile`, `color_disagrees`, `not_power_of_two`, `glyphs_on_empty` |
| APK-протокол | код готовий, **НЕ підключений** | `ondevice/*`: packet(seq, uncertain, flags, device_ts_ms, crc), `discarded_bytes` |

**Головний висновок:** search-бік і базовий per-move timing покриті добре; **device/vision-надійність,
стан-машина і per-game порівняльні метрики — сліпі зони**, а APK-шлях телеметрично «голий».

---

## 2. Таблиця покриття «є / бракує» по зонах

### 2.1 Live per-move
| Сигнал | Є? | Де зараз | Бракує / нотатка |
|--------|----|----------|------------------|
| Обраний хід + eval-деталі | ✅ | `moves` + `eval_details` | повне (dir, eval_ms, engine_ms, prefetch_hit, depth) |
| Фазовий таймінг ходу | ✅ | `eval_details.timing` (5 фаз) | реалізовано; **лічильників немає** (див. нижче) |
| predict-gate hit/miss + frames | ⚠️ | structlog `live.predict_gate_hit/_fallback` | **не в БД**, не агрегується — а це ключ early-spawn (див. `latency_telemetry.md`) |
| Лічильники кадрів/спроб | ❌ | — | `capture_count`, `settle_frames_used`, `parse_retries_used`, `swipe_attempts` спроєктовані, **не пишуться** |
| move_rejected ретраї (скільки/чому) | ⚠️ | structlog `live.swipe_unregistered` + `MOVE_REJECTED` alert | кількість свайп-спроб на **успішному** ході не зберігається |
| Swipe-параметри/прийняття | ❌ | `SwipeParams` повертається і **відкидається** (`_play_move`, рядок ~1226) | геометрія/тривалість людяного свайпу не логуються — свайп не відтворити |
| capture_kind (scrcpy vs screencap) | ❌ | `StreamingCaptureDevice` знає, але не віддає | `db latency` бере `experiments.params_json.capture` = **конфіг**, не факт; прихований фолбек на screencap невидимий |
| vision color_fallback частота | ⚠️ | structlog per-cell | **не рахується**: скільки клітин/хід через колір, які клітини/експоненти «нечитабельні» |
| Timing на аборт-ходах | ❌ | аборт не пише `moves` | `latency_telemetry.md §2.1` радив емітити timing і у WARN аборту — не зроблено (повільний-але-впав хід губиться) |
| Spawn (клітина/значення) | ✅ | `spawns` | повне |

### 2.2 APK-шлях (інтегрується; зараз host не підключений до live)
| Сигнал | Є? | Нотатка |
|--------|----|---------|
| seq пакетів / пропуски / дублі | ❌ | `BoardPacket.seq` є у wire, споживача-лічильника гепів немає |
| RTT / one-way latency | ❌ | `device_ts_ms` є у пакеті, **ніхто не порівнює** з host-recv-часом |
| FLAG_* стани | ❌ | `SECURE_SUSPECTED / NOT_IN_GAME / HAS_UNCERTAIN / BOARD_CHANGED / FRAME_FOLLOWS` не рахуються |
| projection-здоров'я (black/uncertain-storm) | ⚠️ | on-device детектор і `is_black_frame` є; host не агрегує частоту |
| CRC-фейли / desync | ⚠️ | `ProtocolDemuxer.discarded_bytes` рахується, логиться **лише раз на EOF** |
| Розриви / reconnect | ❌ | `SamplerLink` EOF → None; лічильника реконектів/аптайму лінка немає |

### 2.3 Стан-машина / інциденти
| Сигнал | Є? | Нотатка |
|--------|----|---------|
| game-over / win / restart | ✅ | end_reason (`game_over/win_2048/interrupted/...`) + GAME_OVER details |
| Переходи станів (game/popup/auth/not_in_game) | ⚠️ | стан лише в момент **алерту** (`alert.screen_state`); таймлайну спостережень немає |
| Транзитні ре-полли (board dip) | ⚠️ | structlog `supervisor.transient_repoll/_recovered` — **не в ops**, не рахується |
| ERROR-recovery / recovered | ✅ | ops `recovered` + ERROR alert |
| DEVICE_LOST / reconnect | ⚠️ | LOOP_ERROR alert + `live.reconnect_failed` (structlog); успіх/невдача реконекту не чиста метрика |
| AUTH wall / resume vs reset | ✅ | ops `RESUMED` / `interrupted`, notify |
| RESTARTED (нова гра) | ⚠️ | лише notify+log; **не ops-подія** |
| tile_learned (донавчання) | ⚠️ | structlog + notify; не в ops-агрегаті (скільки донавчань/кампанію) |

### 2.4 Per-game / кампанія-агрегати
| Сигнал | Є? | Нотатка |
|--------|----|---------|
| score / moves / duration / max-tile / end_reason | ✅ | `games` + GAME_OVER details |
| Фінальна гістограма плиток | ✅ | GAME_OVER `tile_histogram` |
| peak-1024 / коли вперше досягнуто | ⚠️ | **derivable** з `moves.board_after`, але прямого поля нема (незручно порівнювати 30–50 ігор) |
| forced-2048 / чому закінчилась | ✅ | end_reason розрізняє win_2048 vs game_over vs аборти |
| max-tile timeline | ⚠️ | derivable з moves; агрегату (ply/секунда першого 512/1024/2048) немає |
| moves/s, score/s (метрика запису — timer-режим) | ⚠️ | derivable (`num_moves/duration_s`), не зберігається |
| Per-game надійність (gate hit%, fallback-count, swipe-retry, mean move ms) | ❌ | немає — саме це відрізняє «здорову» гру від «шарпаної» у кампанії |

### 2.5 Sim / experiment
| Сигнал | Є? | Нотатка |
|--------|----|---------|
| Повний запис (moves+spawns+games) | ✅ | `play_game` — та сама контрактна телеметрія |
| Хвіст (розподіл peak-tile) | ✅ | queryable по `games.max_exponent` |
| Density (порожні клітини в часі) | ⚠️ | derivable з board-снапшотів; готового поля/звіту нема |
| end_reason `tile_cap` (2048-стоп) | ✅ | окремий reason у sim |

---

## 3. Ранжовані пропозиції (must-add зверху)

Формат: **ЩО** · **ДЕ** (A=eval_details / B=ops details / C=structlog) · **цінність** · **дешево?**

### MUST

**M1. Vision-надійність per-move: лічильник fallback-клітин + маска.**
ЩО: `vision_fallbacks` (к-сть клітин, вирішених `method="color"`), `vision_unknown` (к-сть `unknown`),
`vision_fallback_mask` (16-біт), `min_confidence`. ДЕ: **A** — додати в `eval_details["timing"]`
сусідні ключі (або окремий `eval_details["vision"]`). Джерело: `read_board` уже вміє
`parse_board_detailed` (`CellRead.method/confidence`); треба щоб ін'єктований `BoardReader`
повертав лічильник (через уже наявний `CellsReader`/`read_cells`, який рахує `suspect`).
ЦІННІСТЬ: **must** — на 50k без нагляду це головний предиктор тихого дрейфу палітри/оклюзій;
дає per-game «скільки ходів бот читав дошку по кольору» і **які експоненти** ламаються (сигнал «пора recalibrate/add-glyphs»).
ДЕШЕВО: так (лічильники вже рахуються у vision; 4 int/float у вже-наявний JSON).

**M2. predict-gate результат у БД (hit/miss + frames + fallback-причина).**
ЩО: `gate_hit` (bool), `gate_frames` (int), `gate_fallback` (bool). ДЕ: **A** — `eval_details["timing"]`.
Зараз лише `log.debug/info` (`live.predict_gate_hit/_fallback`) — губиться. ЦІННІСТЬ: **must** —
це метрика №1 з early-spawn-ресерчу; без неї «середній settle» бреше (змішує gate-hit і legacy-settle),
і не видно деградації розпізнавання спавну наживо. ДЕШЕВО: так (3 поля, значення вже пораховані в `_predict_gate`).

**M3. Лічильники кадрів/спроб ходу.**
ЩО: `capture_count`, `settle_frames_used`, `parse_retries_used`, `swipe_attempts`, `capture_kind`.
ДЕ: **A** — `eval_details["timing"]` (спроєктовано в `latency_telemetry.md §2.2`, **не реалізовано**).
`capture_kind` мусить бути **фактичним** джерелом кадру (scrcpy/screencap) з `StreamingCaptureDevice`,
а не конфігом — інакше тихий фолбек на screencap-WiFi (0.65 с/кадр) невидимий у p95. ЦІННІСТЬ: **must** —
без лічильників фази 5–7 (суми по кадрах) не інтерпретуються; `capture_kind` ловить приховану слабину транспорту.
ДЕШЕВО: так; `capture_kind` потребує тонкого проброса прапора з capture-шару (маленька зміна, не контракт).

**M4. Timing/діагностика на аборт-ходах (не тільки на committed).**
ЩО: емітити той самий timing+counters dict у WARN-логах `live.game_capture_aborted` та в
`_play_move` на `vision_mismatch`/`move_rejected`, плюс покласти у ops `details_json` того ж інциденту.
ДЕ: **B/C** (аборт не пише `moves`, тож A недоступний). ЦІННІСТЬ: **must** для shakedown —
«повільний-але-впав» хід зараз зникає без сліду часу; це найцінніші випадки для дебагу. ДЕШЕВО: так (dict вже збирається `_MovePhaseTimer`).

**M5. Swipe-параметри прийнятого ходу.**
ЩО: `SwipeParams` (start/end xy, duration_ms, jitter) фактичного свайпу, що зареєструвався.
ДЕ: **A** — `eval_details["swipe"]` (компактно: dur_ms + 4 координати). Зараз повертається і **відкидається**
(`_play_move` не забирає результат `device.swipe_direction`). ЦІННІСТЬ: **must** для повного replay
живого ходу (контракт «гра відтворюється постфактум» вимагає знати, ЧИМ саме свайпнули) і для дебагу «свайп не зареєструвався».
ДЕШЕВО: так (значення вже пораховане Humanizer'ом і повертається).

### NICE

**N6. Per-game агрегати надійності в GAME_OVER.details_json.**
ЩО: додати до наявного `GAME_OVER` ops-details: `gate_hit_pct`, `vision_fallback_moves`, `swipe_retry_total`,
`mean_move_ms`, `p95_move_ms`, `moves_per_s`, `score_per_s`, `first_1024_ply`, `first_2048_ply`, `capture_screencap_frac`.
ДЕ: **B** — `_end_game` уже пише details; додати ключі (раннер тримає лічильники за гру). ЦІННІСТЬ: **nice→must для кампанії** —
це і є «порівняти ігри кампанії» одним `SELECT` без реконструкції по `moves`. ДЕШЕВО: так (акумулятори за гру, копійки).

**N7. Стан-таймлайн на переходах (не кожен кадр).**
ЩО: ops-подія `STATE_CHANGE` при зміні `ScreenState` (game→popup→auth→not_in_game→game) з `from/to/board_match_frac`;
+ рахувати transient board-dips (`supervisor.transient_repoll`) у лічильник, зливати в GAME_OVER details.
ДЕ: **B** (переходи) + **C** уже є. ЦІННІСТЬ: nice — стабільність екрана в кампанії, локалізація «де зависло».
Не логувати кожен probe (шум у потоці логів). ДЕШЕВО: так (лог лише на зміні).

**N8. RESTARTED та tile_learned як ops-події.**
ЩО: `RESTARTED` (нова гра після фіналу) і `TILE_LEARNED` (донавчання палітри: експонента/клітина) у `live_events`.
ДЕ: **B**. Зараз лише notify/structlog. ЦІННІСТЬ: nice — «скільки нових плиток донавчено за кампанію» / межі ігор без реконструкції. ДЕШЕВО: так.

**N9. APK-лінк health (коли підключать host у live).**
ЩО (host-сторона, `SamplerLink`/демультиплексор): `seq_gaps`, `seq_dups`, `discarded_bytes` (періодично, не лише на EOF),
`rtt_ms`/`owd_ms` (host_recv − `device_ts_ms`, з калібруванням дрейфу), лічильники `FLAG_*`
(`SECURE_SUSPECTED/NOT_IN_GAME/HAS_UNCERTAIN/BOARD_CHANGED`), `reconnects`, `link_uptime_s`, частота `INCIDENT`-кадрів.
ДЕ: **B** (періодичний ops `APK_HEALTH`) + **C** для потоку. ЦІННІСТЬ: nice зараз (не підключено) → **must у момент інтеграції APK** —
без цього стабільність APK наживо невидима. ДЕШЕВО: так (лічильники в демультиплексорі; RTT — одне віднімання на пакет).
Нотатка: host explicitly НЕ підключений (`ondevice/host.py` docstring) — це заготовка на інтеграцію.

**N10. Sim-хвіст: density/peak-звіт (аналітика, не новий запис).**
ЩО: CLI-звіт по вже-наявних даних: розподіл `max_exponent`, частка ігор ≥1024/≥2048, крива empty-cells по ply
(з `moves.board_after`). ДЕ: читач (як `db latency`), запису не треба. ЦІННІСТЬ: nice — вибір стратегії по хвосту. ДЕШЕВО: так (лише SQL/reader).

**N11. Tier B — індексовані колонки (тільки якщо JSON-звіт почне гальмувати).**
ЩО: винести найгарячіші поля (`verify_ms`, `capture_ms`, `move_ms`, `capture_kind`, `gate_hit`) у NULLABLE-колонки `moves`.
ДЕ: **[КОНТРАКТНА ЗМІНА]** — зачіпає `recording.py`+`schema.py`+`sqlite_recorder.py` (Tier B, `latency_telemetry.md §2.3`).
ЦІННІСТЬ: nice — лише коли per-move JSON-екстракт на мільйонах рядків стане вузьким. ДЕШЕВО: механічно, але контрактне — не робити передчасно.

---

## 4. Топ-5 «додати першими»

1. **M2 — predict-gate hit/frames у `eval_details` (A).** Найдешевше, значення вже пораховані; розблоковує чесний latency-звіт і бачення деградації спавну. Головна метрика early-spawn.
2. **M1 — vision fallback-лічильник + маска (A).** Ранній детектор тихого дрейфу vision на 50k без нагляду; каже, які експоненти пора recalibrate.
3. **M3 — лічильники кадрів/спроб + фактичний `capture_kind` (A).** Робить наявні 5 timing-фаз інтерпретованими і ловить прихований screencap-фолбек (слабина №2 гіпотези).
4. **M4 — timing/diag на аборт-ходах (B/C).** Найцінніше для shakedown: «повільний-але-впав» хід зараз зникає безслідно.
5. **N6 — per-game агрегати надійності в GAME_OVER.details (B).** Одним `SELECT` порівнюєш 30–50 ігор кампанії (gate%, fallback, swipe-retry, moves/s, first-1024) без реконструкції по `moves`.

Спільне для всіх п'яти: **нуль змін у заморожених контрактах**, все у вже-наявні вільні JSON-бакети
(`eval_details_json` / `live_events.details_json`), значення переважно вже пораховані в раннері/vision —
лишається «запечатати» їх у dict у `_commit_move` / `_end_game` / аборт-гілках.

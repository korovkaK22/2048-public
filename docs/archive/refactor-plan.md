# Refactor plan — розвантажити god-класи, зробити зміни зручнішими

Складено з двох read-only аудитів коду 2026-07-11. **Реалізовувати в ОКРЕМІЙ гілці**
(`refactor/*`), по одному кроку = один коміт, найбезпечніше першим. Для будь-чого, що
торкається живого пайплайну (ObservationGate/AuthFlow/SwipeController) — жива перевірка на
телефоні перед мержем.

**Заморожені контракти — не чіпати без узгодження:** `core/types.py`, `recording.py`.
Пункти, що до них наближаються, позначені ⚠.

## Зона A — `runner/` + `device/`
`LiveRunner` (live.py, **2473 рядки**, ~35 методів, 24 залежності в __init__) = god-клас з ~11
відповідальностей. Периферія (`humanizer/adb/apk_capture/bg_detect/newgame/auth/escalation/
supervisor`) вже когезивна — НЕ чіпати.

**TOP-3 безпечні (чистий extract, 0 зміни поведінки):**
1. `runner/move_telemetry.py` — винести всю module-level телеметрію (`_PhaseSpan`, `_MovePhaseTimer`,
   `_MoveCounters`, `_TimedDevice`, `_TimedDetector`, `_GameTelemetry`, `_percentile`, `_board_stats`, ~500 рядків). −20% з live.py.
2. `runner/live_config.py` — винести `LiveConfig` + константи з re-export (cli.py не міняється).
3. `runner/outcomes.py` — `_WinPopup2048`/`_GameOverPopup`/`_GameReset`/`_abort_reason`/`diff_spawn` (re-export `diff_spawn`!).

**Далі (середній ризик, ПІСЛЯ 1-3 + жива перевірка):**
- R4 `ObservationGate` (`runner/observation.py`) — перцепція: capture/settle/predict-gate/APK-observe (~400 рядків). Серце fail-closed — обережно з підняттям popup-винятків і `_last_frame`.
- R5 `AuthFlow` — винести `_handle_auth`, узгодити з наявним `Supervisor.auth_resolver` (тепер під'єднаний restart-auth фіксом).
- R6 `SessionRecovery` — never-crash outer loop.
- R7 `SwipeController` (`_play_move`) — ОСТАННІМ (залежить від R4/телеметрії).

**Дешеві coupling-фікси (незалежні):**
- Публічний `StreamingCaptureDevice.last_capture_kind()` замість duck-typing приватних полів у `_device_capture_kind`.
- Публічний `incidents.write_png()` замість приватного `_write_png`.

## Зона B — `vision/ai/storage/sim`
**TOP-3 безпечні:**
1. **Уніфікувати hex-кодек дошки** — 2 копії (`storage/schema.py` + `ai/go_engine.py`) → `core/boardhex.py`; тонкі обгортки лишають свої типи помилок.
2. **Виділити `vision/crop.py`** — pixel/geometry-примітиви з `parser.py` (cell_crop, mean_patch_color, nearest_palette_entry, tile_background_color); states.py перестає тягнути весь парсер.
3. **Одне джерело калібрування + drift-guard тест** — `reference.py` = авторитет; тест, що `data/calibration.json` і `ondevice-apk/.../calibration.json` == згенерованого. ⚠ **ФАКТИЧНИЙ ДРЕЙФ ВЖЕ Є:** APK-копія має палітру 512/1024, яких нема в reference, і НЕ має game_over-анкерів. Звірити з власником APK.

**Далі:**
- ⚠ Типізувати `eval_details`-контракт (`telemetry_keys.py` + TypedDict) — ключі течуть ai→sim→storage→latency без типів; зміна ключа тихо ламає `db latency`. Вміст dict не заморожений, але сигнатуру `recording.py` не міняти.
- Розбити `ai/go_engine.py` (517) → `engine_binary.py` + `engine_client.py` + Strategy (Decision-конструкцію лишити в Strategy).
- `core/bitboard.py` — явний внутрішній API замість underscore-імпортів `_pack/_move_bb/_transpose` в expectimax/heuristics (обов'язковий бенч на тихій машині).
- Розбити `tgnotify.py` (1178) на пакет (transport/config першими).
- ⚠ Ops не пише `games` напряму (`ops_schema.mark_interrupted_games`) — за санкціонований хелпер.
- Прибрати глобал `parser.py:_color_disagrees_seen`.

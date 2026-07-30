# Прискорення тестового сюїту — заміри й план

> Ресьорч-звіт: заміри й план прискорення. На момент написання нічого не було змінено.
> Сирі логи вимірів (raw stdout pytest) у репозиторії не зберігаються — нижче зведені числа.
> Дата: 2026-07-11. Хост: Windows 10, 8 логічних ядер (`nproc=8`), Python 3.12.3, `.venv`.

---

## 0. TL;DR — топ-5 важелів за виграшем

| # | Важіль | Виграш | Ризик | Коли |
|---|--------|--------|-------|------|
| 1 | **Увімкнути `-n auto` з груповою ізоляцією важких тестів** (xdist_group + `--dist loadgroup`) | **55.8s → ~10-12s (~5x)** | середній (потрібні кроки 2-4, інакше повільніше+флейкі) | після кроків 2-4 |
| 2 | **`pythonpath = "."` у pytest-конфіг** — зараз сюїт взагалі НЕ збирається через `./.venv/Scripts/pytest` (тільки через `python -m pytest`) | розблоковує CI/тулінг; 0 щодо швидкості | нульовий | **безпечно негайно** |
| 3 | **Згрупувати ffmpeg-декод тести (scrcpy+minicap) на один воркер** — без цього наївний `-n auto` = 46s і падає | усуває контеншн, що робить `-n8` ПОВІЛЬНІШИМ за `-n2` | низький | безпечно (нові маркери/conftest) |
| 4 | **Полагодити 3 xdist-небезпечні тести** (`test_parse_command`, tgnotify-таймінг, `sleep(0.5)` у prefetch) | прибирає флейки, що блокують дефолтний `-n auto` | середній (треба debug) | частково блоковано (див. нижче) |
| 5 | **Зменшити лічильники реальних пошуків** у go/expectimax тестах | ~5-6s на серійному критичному шляху go-групи | низький | низький |

**Головний результат:** чиста Python-маса сюїту (367 тестів без device/tgnotify/go) під `-n auto` **виконується за 6.58s** проти 55.8s серійно. Тобто ~99% тестів ідеально паралеляться; усе гальмо — жменька ffmpeg/subprocess/socket тестів, які треба **ізолювати на окремий воркер**, а не пришвидшувати поштучно.

---

## 1. Базові заміри (wall-clock, повний сюїт)

| Конфіг | Час | Результат | Нотатка |
|--------|-----|-----------|---------|
| серійно | **55.8s** | ✅ pass | baseline |
| `-n 2` | **22.5s** | ❌ 2 failed | найшвидший *наразі*, але флейкі |
| `-n 4` | **40.2s** | ❌ 2 failed | ПОВІЛЬНІШЕ за `-n2` |
| `-n auto` (8) | **46.2s** | ❌ 3 failed | ще повільніше |
| `-n auto`, лише Python-маса (без device/tgnotify/go) | **6.58s** (367 тестів) | ❌ 1 failed* | цільовий орієнтир для маси |

\* падіння = той самий `test_parse_command` (не контеншн — див. §3.3).

**Ключове спостереження:** більше воркерів = ПОВІЛЬНІШЕ (`-n2` 22.5s → `-n8` 46.2s). Це діагностична підпис контеншну: ffmpeg-декод тести з внутрішнім `first_frame_timeout_s=10.0` (scrcpy) при браку CPU під 8 воркерами не встигають стартувати ffmpeg → ідуть у retry-ланцюг по ~10s кожен, роздуваючи хвіст. Тобто наївний `-n auto` не лише флейковий, а й програє серійному по швидкості, доки важкі тести не ізольовані.

> **Важлива операційна знахідка (у ops-playbook варто занести):** запускати ТІЛЬКИ через
> `./.venv/Scripts/python.exe -m pytest`. Через console-скрипт `./.venv/Scripts/pytest`
> сюїт **падає на збірці** з `ModuleNotFoundError: No module named 'tests'` (8 модулів
> роблять `from tests.<pkg>.conftest import ...`, а `tests/` не є importable без
> `project/` у `sys.path`; `python -m` додає cwd, скрипт — ні). Фікс — `pythonpath="."`
> (крок 2), тоді працюють обидва launcher-и. Перевірено: `pytest -o pythonpath=.` збирає.

---

## 2. Топ-30 найповільніших тестів (серійний прогін, call-time)

Джерело: `research/test_speed/run_full_module.txt` (`--durations=50`).

| # | s | Тест | Файл | Причина |
|---|-----|------|------|---------|
| 1 | 6.20 | `test_choose_returns_legal_move` | ai/test_go_engine.py | **50 реальних depth-3 пошуків** через Go-subprocess (IPC-цикл `while checked < 50`, ~L? нижче) |
| 2 | 3.81 | `test_expectimax_beats_random_by_wide_margin` | ai/test_expectimax.py | **справжня стат-сим**: 8 seed × (expecti depth2 + random), `max_moves=250` (L~ у тілі) |
| 3 | 1.91 | `test_capture_fails_cleanly_when_stream_never_comes_back` | device/test_scrcpy.py | реальні таймаути failure-path: `connect_attempts=2 × connect_timeout=0.3 + first_frame_timeout=2.0` + ffmpeg (L387) |
| 4 | 1.35 | `test_reconnects_after_stream_death` | device/test_scrcpy.py | ffmpeg-декод + reconnect-очікування (L371) |
| 5 | 1.02 | `test_connect_failure_raises_minicap_error` | device/test_minicap.py | реальний TCP connect-timeout/retry на мертвий порт |
| 6 | 1.02 | `test_connect_failure_cleans_up_server_and_forward` | device/test_scrcpy.py | `connect_attempts=2 × connect_timeout=0.5` до мертвого порту (L291) |
| 7 | 0.88 | `test_fresh_start_clears_secure_state` | device/test_scrcpy.py | 2 ffmpeg-пайплайни (black→green), `@requires_ffmpeg` (L502) |
| 8 | 0.83 | `test_eof_flushes_all_frames_through_decoder` | device/test_scrcpy.py | ffmpeg-декод + EOF-flush (L362) |
| 9 | 0.79 | `test_capture_returns_latest_frame_not_first` | device/test_scrcpy.py | ffmpeg-декод + `wait_until(frames>=3)` (L338) |
| 10 | 0.74 | `test_start_capture_and_full_teardown` | device/test_scrcpy.py | ffmpeg spawn+decode + очікування завершення потоків (L311) |
| 11 | 0.74 | `test_sustained_black_run_trips_secure_error` | device/test_scrcpy.py | ffmpeg-декод 6 чорних кадрів (L460) |
| 12 | 0.71 | `test_cropped_pipeline_decodes_crop_sized_frames` | device/test_scrcpy.py | ffmpeg encode+decode crop-стріму (L592) |
| 13 | 0.69 | `test_non_black_frame_resets_black_counter` | device/test_scrcpy.py | ffmpeg-декод 5 кадрів (L478) |
| 14 | 0.67 | `test_plays_full_game_via_runner` | ai/test_go_engine.py | повна гра через Go-subprocess IPC |
| 15 | 0.65 | `test_full_cycle_over_http` | tgnotify/test_tgnotify.py | **реальний `ThreadingHTTPServer`** + poll-цикл `sleep(0.05)` до 5s (L500-536) |
| 16 | 0.61 | `test_black_detection_disabled_when_limit_zero` | device/test_scrcpy.py | ffmpeg-декод 8 чорних (L490) |
| 17 | 0.54 | `test_network_error_has_no_url_or_token` | tgnotify/test_tgnotify.py | реальний network-error шлях |
| 18 | 0.50 | `test_engine_throughput_floor` | core/test_perf.py | **навмисний timed-бенчмарк**: 60k ходів у циклі, `perf_counter` (L37-56) — таймінг-чутливий |
| 19 | 0.42 | `test_partial_reads_are_reassembled` | device/test_minicap.py | socket-трикл reassembly (`chunk_delay` sleep) |
| 20 | 0.28 | `test_parse_on_crop_frame_matches_full_frame[board_a.png]` | device/test_crop.py | cv2-parse на crop-кадрі |
| 21 | 0.26 | `test_watch_sends_only_new_error_fatal` | tgnotify/test_tgnotify.py | file-watch шлях |
| 22 | 0.25 | `test_bench_smoke` | sim/test_cli.py | малий bench через CLI |
| 23 | 0.23 | `test_parse_on_crop_frame_matches_full_frame[board_b.png]` | device/test_crop.py | cv2-parse |
| 24 | 0.23 | `test_sampler_vs_parser_on_real_screen[...UNEXPECTED-2/after]` | ondevice/test_ondevice_fixture_screens.py | cv2-parse реального скріна |
| 25 | 0.23 | `test_make_strategy_go_uses_engine_and_fills_move_telemetry` | runner/test_cli_strategy.py | Go-движок телеметрія |
| 26 | 0.23 | `test_move_parity_with_python_engine` | ai/test_go_engine.py | **500 бордів** Go↔Py через IPC (L56-60) |
| 27 | 0.22 | `test_chunked_trickled_stream_reassembles` | device/test_scrcpy.py | ffmpeg + trickle (L353) |
| 28 | 0.22 | `test_parse_on_crop_frame_matches_full_frame[board_c.png]` | device/test_crop.py | cv2-parse |
| 29 | 0.22 | `test_sampler_vs_parser_on_real_screen[...UNEXPECTED/after]` | ondevice/test_ondevice_fixture_screens.py | cv2-parse |
| 30 | 0.21 | `test_win_anchors_are_optional` | vision/test_states.py | cv2 |

**Розподіл причин у топ-30:** ffmpeg-декод (scrcpy+minicap) — 13 тестів (~10s); Go-subprocess IPC — 4 тести (~7.8s); реальна sim (expectimax) — 1 (3.8s); реальний HTTP/network (tgnotify) — 3 (~1.5s); timed-бенчмарк — 1 (0.5s); cv2-parse — решта дрібні (<0.3s). **Немає** жодного гальма від `time.sleep` як «пустого простою» (крім навмисного poll-sleep і одного `sleep(0.5)` у prefetch, §6) і **немає** hypothesis-гальм (§6).

---

## 3. Паралельна безпека — що саме ламає `-n auto` і як полагодити

Три класи проблем. Порти вже НЕ проблема: `FakeStreamServer`/`FakeMinicapServer` біндяться на **ефемерний порт 0** (`bind(("127.0.0.1", 0))`, test_scrcpy.py:126, test_minicap.py:100) — колізій портів немає за дизайном. Реальні блокери інші:

### 3.1. ffmpeg-декод контеншн (scrcpy + minicap) — ГОЛОВНИЙ блокер

**Доказ:** `research/test_speed/xdist_auto.txt` — `scrcpy.capture_attempt_failed attempt=1 error='no decoded frame within 10.0s'` та `[WinError 10038] not a socket`. Ізольовано ці тести **проходять**: `test_scrcpy.py` під `-n4` → `44 passed in 9.75s`, `test_vision_cli` під `-n2` → pass. Падають ЛИШЕ у повному `-n auto`, коли 8 воркерів одночасно спавнять реальні ffmpeg-процеси → CPU голодує → ffmpeg не встигає видати перший кадр за `first_frame_timeout_s=10.0` (коментар у test_scrcpy.py:88-92 прямо описує повільний старт ffmpeg на Windows під навантаженням) → retry-ланцюг × ~10s.

**Не потрібно** прискорювати кожен тест. Потрібно **не давати їм спавнити ffmpeg одночасно**: помістити всі `@requires_ffmpeg` тести scrcpy + декод-тести minicap у **один xdist_group** → вони йдуть послідовно на ОДНОМУ воркері (~10s разом), поки інші 7 воркерів жують Python-масу. Storm зникає.

Тести під групу (файл `tests/device/test_scrcpy.py`, всі з `@requires_ffmpeg`): `test_start_capture_and_full_teardown`, `test_capture_returns_latest_frame_not_first`, `test_chunked_trickled_stream_reassembles`, `test_eof_flushes_all_frames_through_decoder`, `test_reconnects_after_stream_death`, `test_capture_fails_cleanly_when_stream_never_comes_back`, `test_sustained_black_run_trips_secure_error`, `test_non_black_frame_resets_black_counter`, `test_black_detection_disabled_when_limit_zero`, `test_fresh_start_clears_secure_state`, `test_cropped_pipeline_decodes_crop_sized_frames`, `test_cropped_config_rejects_full_size_stream` + аналогічні декод-тести `test_minicap.py`.

### 3.2. tgnotify — таймінг/спільний cwd-шлях

**Доказ:** `full_n2.txt` — `FAILED test_reminder_fires_until_acked` (падає лише 1 з 3 паралельних прогонів → інтермітентно). Тест сам по собі детермінований (маніпулює `last_sent_mono` напряму, без реального sleep, L~ `_maybe_remind`), але `make_notifier`/`.secrets/`-walk-up і журнали резолвляться відносно cwd (ops-playbook §54-55) — під xdist кілька воркерів у спільному cwd можуть контендити на спільному шляху. Плюс `test_full_cycle_over_http` тримає реальний HTTP-сервер. **Фікс:** ті самі xdist_group («serial_net») для всіх tgnotify-тестів, і/або гарантувати, що ВСІ шляхи scoped у `tmp_path`.

### 3.3. `test_parse_command` (vision CLI) — крос-модульний global-state / xdist-env

**Найцікавіший.** Падає у КОЖНОМУ повному паралельному прогоні (`-n2`, `-n4`, `-n auto`), у т.ч. у чистій Python-масі БЕЗ ffmpeg (`pure_python_nauto.txt`, 6.58s — тобто це НЕ CPU-контеншн). Але:
- ізольовано `test_vision_cli.py` під `-n2` → **pass** (2/2);
- серійно `pytest tests/<M> test_parse_command` для M ∈ {core, ai, sim, ondevice, runner, device, vision} → **pass у всіх** (бісекція за модулями не відтворює);
- підмножина `vision+ondevice+ai` під `-n2 -vv` → **247 passed** (не відтворює).

Отже тригер вимагає **повного набору модулів під xdist-розподілом** (core/sim/storage/runner присутні), і НЕ відтворюється ані серійно, ані у підмножинах. Це вказує на **крос-модульний leak global-state АБО різницю оточення воркера** (rich/typer console-width, env, чи process-global на кшталт `cv2.setNumThreads`/numpy-стан), який спливає лише коли xdist кладе певний тест-попередник на той самий воркер перед `test_parse_command`. Тест ассертить **точний 16-tuple підрядок** у текстовому output CLI (`test_vision_cli.py:86`), тому крихкий і до реального миспарсу, і до переносу рядка в output.

**Рекомендація власнику (діагностика, не наосліп):** відтворити на повному `-n2 -vv`, витягнути `result.output` на падінні (`--pdb`/`-l`), щоб розрізнити *wrapped-output* vs *реальний миспарс*. Робастний фікс: ассертити **структуровані дані** (JSON-вивід CLI або повертати кортеж), а не точний підрядок у rich-тексті; або зафіксувати console-width. Інтерим: помістити `test_parse_command` (чи модуль) в окремий xdist_group.

### 3.4. core/test_perf throughput floor — таймінг-ризик (наразі не падає)

`test_engine_throughput_floor` (60k ходів, `perf_counter`, floor 20k/s при таргеті 50k) під паралеллю ділить CPU → moves/s падає. Наразі витримав (`-n2/-n4/-n8` не показали його падіння — floor консервативний), але це прихований флейк на завантаженому боксі. **Рекомендація:** покласти в ffmpeg-серійну групу (він і так не має конкурувати за CPU), щоб мірявся на вільному воркері.

---

## 4. Рецепт «як увімкнути `-n auto` без флейкі»

xdist 3.8.0 (перевірено) підтримує `@pytest.mark.xdist_group` + `--dist loadgroup`. **Найменш-колізійний варіант — централізувати все в ОДНОМУ новому `tests/conftest.py` + `pyproject.toml`, не чіпаючи per-file тест-модулі** (їх тримають інші агенти).

**Крок A — `pyproject.toml` `[tool.pytest.ini_options]`:**
```toml
pythonpath = "."                       # крок 2: лагодить збірку через bare `pytest`
addopts = "-q --durations=15 -n auto --dist loadgroup"
markers = ["ffmpeg_serial: важкі ffmpeg/socket/timed тести — один воркер"]
```

**Крок B — новий `tests/conftest.py` (централізована ізоляція, 0 правок у тест-файлах):**
```python
import pytest

_SERIAL_PATH_SUBSTR = ("device/test_scrcpy", "device/test_minicap",
                       "tgnotify/", "core/test_perf")

def pytest_collection_modifyitems(items):
    for it in items:
        p = it.nodeid.replace("\\", "/")
        if any(s in p for s in _SERIAL_PATH_SUBSTR):
            it.add_marker(pytest.mark.xdist_group("serial_heavy"))
```
Це кладе всі ffmpeg/socket/HTTP/timed тести в один xdist-груп «serial_heavy» → вони йдуть послідовно на одному воркері, решта паралеляться. `test_parse_command` лишається окремою задачею (§3.3): до її фіксу — або додати `"vision/test_vision_cli"` у список, або тимчасово `--deselect` у CI.

**Очікуваний час:** маса ~6.6s ∥ serial_heavy-воркер (scrcpy ~10s + minicap + tgnotify + perf) ∥ go-тести (~8s на своєму воркері) → wall ≈ **max(гілок) ≈ 10-12s**. Тобто **55.8s → ~10-12s**.

> Примітка: go-engine тести (§2 #1,14,26) НЕ треба серіалізувати — кожен воркер спавнить власний Go-subprocess (module-scoped фікстура `strategy`, test_go_engine.py:33-42), вони parallel-safe, лише CPU-важкі. Їх природно розкидає `loadgroup` по вільних воркерах.

---

## 5. План оптимізації по файлах-власниках (виграш / ризик / коли)

План упорядкований за співвідношенням виграш/ризик: спершу зміни, що не торкаються самих тестів (конфіг), потім тримінг окремих тестів.

| Зміна | Файл(и) | Виграш | Ризик | Статус |
|-------|---------|--------|-------|--------|
| **`pythonpath="."`** (лагодить bare `pytest`) | `pyproject.toml` | розблокування CI/тулінгу | нульовий (перевірено) | ✅ **безпечно негайно** |
| **`addopts += -n auto --dist loadgroup`** | `pyproject.toml` | 55.8s→~10-12s | середній (тільки з conftest нижче) | ✅ безпечно (після conftest) |
| **Централізований `pytest_collection_modifyitems` → xdist_group «serial_heavy»** | **новий** `tests/conftest.py` | усуває ffmpeg-storm + tgnotify/perf-флейки | низький | ✅ безпечно (новий файл, 0 колізій) |
| Полагодити `test_parse_command` (структурований ассерт) | `tests/vision/test_vision_cli.py` | прибирає останній xdist-флейк | середній (треба debug §3.3) | ⚠️ спершу відтворити |
| Прибрати `time.sleep(0.5)` → poll на `prefetch_hit` | `tests/ai/test_go_engine.py` (L242) | ~0.5s + стабільність | низький | 🔶 після конфігу |
| `test_choose_returns_legal_move`: `50 → 20` перевірок | `tests/ai/test_go_engine.py` | ~3.7s (6.2→~2.5s) | низький (усе ще покриває) | 🔶 після конфігу |
| `test_move_parity`: `500 → 150` бордів | `tests/ai/test_go_engine.py` | ~0.15s | низький | 🔶 після конфігу |
| `test_expectimax_beats_random`: `8 → 5` seed або `max_moves 250→180` | `tests/ai/test_expectimax.py` | ~1.4s (3.8→~2.4s) | низький (2x-margin величезний) | ✅ безпечно |
| tgnotify: гарантувати всі шляхи в `tmp_path` (усуває §3.2 корінь) | `tests/tgnotify/test_tgnotify.py` | стабільність під ∥ | середній | ✅ безпечно |
| scrcpy failure-path: зменшити `connect_timeout`/`first_frame_timeout` де тест і так очікує провал | `tests/device/test_scrcpy.py` | ~1-2s | середній (можна ввести флейк) | ⚠️ device-тека чутлива до таймаутів — обережно |

**Порядок виконання:** (1) `pythonpath` + новий `tests/conftest.py` + `addopts` — негайно, дає весь основний виграш; (2) `test_expectimax` тримінг + tgnotify tmp_path — негайно; (3) go-engine тримінги і scrcpy; (4) `test_parse_command` — окрема debug-задача, до фіксу тримати в serial-групі або deselect.

---

## 6. Hypothesis та `@pytest.mark.slow`

- **Hypothesis НЕ є гальмом.** `grep max_examples|@settings|deadline=` по `tests/` — **0 збігів**; property-тести (`tests/core/test_properties.py`) використовують дефолт `max_examples=100`, але у топ-30 найдорожчий із них — `test_left_on_mirror_is_mirror_of_right` **0.16s**. Рухати `max_examples` вниз НЕ потрібно (втратимо покриття без виграшу). Радше протилежне: після прискорення можна дозволити собі профіль `--hypothesis-profile=thorough` на CI.
- **`@pytest.mark.slow` — не рекомендую як основний важіль.** Прибирати з дефолту go/expectimax/scrcpy = втрачати покриття рушія та захвату саме там, де вони найцінніші, а виграш той самий дає `-n auto`-групування без втрати покриття. Якщо все ж потрібен «швидкий локальний» профіль — маркувати `slow` лише реально дорогі й покрити CI повним прогоном: кандидати — `test_choose_returns_legal_move`, `test_plays_full_game_via_runner`, `test_move_parity_with_python_engine` (ai/test_go_engine.py), `test_expectimax_beats_random_by_wide_margin` (ai/test_expectimax.py). Це опція «на потім», не для завтрашнього залізного прогону.
- **`test_engine_throughput_floor`** — НЕ маркувати `slow` (за коментарем «Skipping is NOT allowed»), але винести на вільний воркер (§3.4), інакше під ∥ таймінг-флейк.

---

## Провенанс (сирі логи у `research/test_speed/`)

- `run_full_module.txt` — серійний `--durations=50` (джерело топ-30).
- `serial_time.txt`, `serial_summary.txt` — серійний wall-clock 55.8s.
- `xdist_auto.txt`, `xdist_time.txt` — `-n auto` (8): 46.2s, 3 failed (scrcpy storm).
- `full_n4.txt`, `full_n2.txt`, `full_n2_repeat.txt` — `-n4`=40.2s/2f, `-n2`=22.5s/2f (демонструють інверсію «більше воркерів = повільніше»).
- `pure_python_nauto.txt` — Python-маса `-n auto` = 6.58s / 367 тестів (цільовий орієнтир; лишається `test_parse_command`).
- `run_full_1.txt`, `run_full_exact.txt` — фіксують поломку збірки через console-скрипт `pytest` (обґрунтування кроку 2).

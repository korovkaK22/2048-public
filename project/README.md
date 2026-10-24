# bot2048 — код

Огляд проєкту, результати й пояснення правил гри — у
[`../README.md`](../README.md). Тут — те, що потрібно, щоб **запустити й
розробляти**.

Архітектура і контракти модулів: [`../docs/architecture.md`](../docs/architecture.md).
Індекс усієї документації: [`../docs/README.md`](../docs/README.md).

## Установка

```bash
cd project
python -m venv .venv                          # Python 3.12+
./.venv/Scripts/python -m pip install -e ".[dev,device]"
```

- `[dev]` — pytest, ruff, mypy, hypothesis.
- `[device]` — `adbutils`, `opencv-python`, `pillow`. Потрібен **і для тестів**:
  частина суїти працює зі справжніми `cv2`/`Pillow` і справжнім ffmpeg (проти
  фейкового телефона, не проти моків). Без цього extra 4 тести падають і 173
  скіпаються.
- Go 1.24+ — для рушія. Збирати вручну не потрібно: `ai/go_engine.py` сам
  викликає `go build`, якщо бінарник відсутній або старший за свої `*.go`.

На Linux/macOS шляхи до venv — `./.venv/bin/...` замість `./.venv/Scripts/...`.

## Запуск

```bash
# симуляція за правилами monobank, чистий Python
./.venv/Scripts/bot2048 sim run --strategy expectimax --games 10 \
    --stop-tile 2048 --db data/games.db

# те саме на Go-рушії з цільовою функцією живої гри
./.venv/Scripts/bot2048 sim run --strategy go-expectimax --farm \
    --stop-tile 2048 --games 50 --db data/games.db

# порівняти всі стратегії таблицею
./.venv/Scripts/bot2048 sim bench

# розібрати результати
./.venv/Scripts/bot2048 db report
./.venv/Scripts/bot2048 db replay <game_id>
./.venv/Scripts/bot2048 db spawn-stats
```

> **`--stop-tile 2048` обов'язковий.** Дефолт CLI симулює *класичний* 2048, де
> гра продовжується після 2048. У клоні monobank 2048 завершує партію, тому без
> цього прапорця числа недосяжні в реальній грі. `--farm` вмикає ту саму цільову
> функцію, що працює в живій грі (доступний лише для `go-expectimax`).

`bot2048 --help` показує під-застосунки: `sim`, `db`, `vision`, `live`.

## Перевірки

```bash
./.venv/Scripts/python -m pytest              # 860 тестів, ~35 с
./.venv/Scripts/python -m ruff check src tests
./.venv/Scripts/python -m mypy src            # strict

# Go-рушії
cd tools/engine2048 && go vet ./... && go test ./...
cd ../ntuple        && go vet ./... && go test ./...
```

`mypy` покриває `src`, не `tests`: у `tests/` немає `__init__.py`, тому mypy
падає на `Duplicate module named "conftest"`. Ruff перевіряє і те, і те.

Тести в `tools/ntuple`, яким потрібні ваги мережі, **скіпаються самі**, якщо
файл ваг відсутній — окремих дій не треба.

## Ваги n-tuple мережі

56 МБ, тому не в git:

```bash
scripts/fetch_weights.sh      # тягне з GitHub Release, звіряє md5 і розмір
```

Без ваг усе інше працює: дефолтний рушій `engine2048` не потребує файлів даних
узагалі. Ваги потрібні лише для гри на n-tuple мережі й для режиму полювання на
рекорд.

## Структура

```
src/bot2048/       код (розбір по модулях — у docs/architecture.md)
tests/             дзеркалить src/
tools/engine2048/  Go: базовий рушій, ручна евристика (дефолт)
tools/ntuple/      Go: n-tuple мережа, TD-навчання, банкір рекорду
ondevice-apk/      Kotlin: MediaProjection-семплер дошки на телефоні
config/experiments/  15 конфігів батч-експериментів
research/          одноразові дослідницькі скрипти + сирі дані (звіти — ../docs/research/)
scripts/           setup_adb.sh, fetch_weights.sh, батч-раннери
data/              games.db (gitignored) + calibration.json
```

Усе (сими, тести, live) працює нативно на хості; Docker у репозиторії немає.

## Гра на телефоні

1. `scripts/setup_adb.sh` — покладе adb і scrcpy в `tools/`.
2. [`../docs/live-setup.md`](../docs/live-setup.md) — USB-debugging, калібрування,
   `bot2048 vision calibrate`.
3. `./.venv/Scripts/bot2048 live play --capture apk`.

> Для живої гри використовуйте `--capture apk`. Бекенд `scrcpy` стоїть дефолтом,
> але H.264-компресія псує порівняння пікселів, коли талісман-кіт застосунку
> заходить на дошку, і прогін помилково зупиняється (див.
> [`../docs/device-ui-notes.md`](../docs/device-ui-notes.md)).

## Конвенції

- Python 3.12+, повні type hints, `mypy --strict`, `ruff` (конфіг у `pyproject.toml`).
- У бібліотечному коді немає `print` — лише structlog; вивід CLI — rich.
- Кожна партія повністю відтворювана з записаних подій
  (`src/bot2048/recording.py`).
- `core/types.py` і `recording.py` — незмінні контракти: на них спираються і код,
  і вже записані в БД дані.

# docs — індекс документації

Точка входу для агентів — розділ «Документація» в [`../CLAUDE.md`](../CLAUDE.md); тут — повний
перелік. У `docs/` лежать лише **живі** документи; датовані звіти — у `research/`, витіснені —
у `archive/`. Код і dev-старт — [`../project/README.md`](../project/README.md); історія проєкту
й результати — [`../README.md`](../README.md).

## Ядро
- [`architecture.md`](architecture.md) — **головний документ**: layout, контракти модулів, схема БД, фази. Читати першим.
- [`components.md`](components.md) — **маніфест компонентів**: реальний граф імпортів, шари import-linter, по компоненту — шляхи, входи, залежності, CI, хто залежить, рецепт видалення; борги меж. Читати перед реструктуризацією чи видаленням.
- [`experiment-log.md`](experiment-log.md) — журнал «config → результат → висновок», найновіші зверху. Дописувати після кожного випробуваного варіанта (merge=union).
- [`training-and-runs-census.md`](training-and-runs-census.md) — зведення: що натренували, скільки прогонів, швидкості, що зайшло / що закрили.
- [`experiments.md`](experiments.md) — харнес батч-експериментів (`scripts/sim/experiment.sh`): формат конфігу, шардинг, злиття, підпапки результатів.

## Телефон, гра, операції
- [`device-ui-notes.md`](device-ui-notes.md) — усі нюанси UI телефона й гри (палітра, безпечні зони, баги застосунку). Нові спостереження — сюди.
- [`rules-research.md`](rules-research.md) — правила клону monobank 2048: встановлені факти й відкриті питання, як міряємо.
- [`edge-cases.md`](edge-cases.md) — чекліст едж-кейсів live-research зі статусами (таймер, попапи, авторизація…).
- [`live-setup.md`](live-setup.md) — від голого хоста до бота в грі: adb, scrcpy, калібрування, скріншоти (ОС-нейтрально; macOS-специфіка — нижче).
- [`macos-setup.md`](macos-setup.md) — **runbook live-хоста Mac mini M4**: brew, venv, рушії arm64, ваги, adb-ключ, USB/WiFi, pmset/caffeinate, launchd, логи, Telegram, чекліст першої години, як читати `db latency` проти еталону i7.
- [`ops-playbook.md`](ops-playbook.md) — операційні граблі: Monitor, фонові процеси (Windows `taskkill` / macOS `kill -TERM`), ADB-по-WiFi, CLI/venv/тести. Читати перед живими прогонами.
- [`ops-monitoring.md`](ops-monitoring.md) — підсистема надійності живих прогонів: алерти, heartbeat, incident-бандли, escalation.
- [`telegram-notify.md`](telegram-notify.md) — Telegram-міст (пакет `tgnotify/`): сповіщення й команди для користувача.
- [`ondevice-vision-design.md`](ondevice-vision-design.md) — дизайн розпізнавання дошки на телефоні (22-байтний board-пакет замість повних кадрів).

## Стратегія й рушій
- [`tail-analysis.md`](tail-analysis.md) — чому довжина партії впирається в стіну: функція ризику, резервуари, A/B на 100 сідах (консолідує архівні аудит і стратегію 2026-07-11).
- [`record-engine.md`](record-engine.md) — міст «банкіра рекорду» в живому рушії: прапорці, маршрутизація, як увімкнути.

## Дослідження — [`research/`](research/README.md)
Датовані звіти (здебільшого 2026-07-10/11): огляд алгоритмів і готових рушіїв, шлях до 50k,
кандидат-стратегії, латентність живого циклу, свайпи, он-девайс обробка, телеметрія, швидкість
тестів, code review. Повний список з описами — у [`research/README.md`](research/README.md).
Скрипти й сирі дані до них — у [`../project/research/`](../project/research/).

## Архів — [`archive/`](archive/README.md)
Витіснені документи, лишені для історії; по кожному — чому заархівовано і що його замінило:
- `2026-07-10-initial-notes.md` — стартовий конспект (класичний 2048, не правила клону).
- `benchmark-report.md` — бенчмарк ранніх Python-стратегій без правила зупинки на 2048.
- `auth-session-research.md` — форензика PIN-екрана на одному пристрої/прогоні.
- `refactor-plan.md` — план R1–R7, здебільшого виконаний.
- `audit-2026-07-11.md`, `strategy-2026-07-11.md` — датовані аудит і стратегія, зведені в `tail-analysis.md`.

## Знання — [`knowledge/`](knowledge/README.md)
Snapshot агентської пам'яті (2026-07-29): стеля скору, закриті глухі кути, операційні правила.
Історичний дамп, не актуальна дока; індекс файлів — у [`knowledge/README.md`](knowledge/README.md).

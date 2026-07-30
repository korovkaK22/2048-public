# Дослідження: причина перелогінів у monobank (com.ftband.mono)

Дата аналізу: 2026-07-10. Пристрій: POCO F3 (alioth_global), Android/MIUI, USB.
Метод: тільки читання — `dumpsys package/activity/usagestats/deviceidle/battery`, `cmd appops get`, `adb logcat -d` (дамп, без стріму). Жодних tap/keyevent/am start/force-stop/settings put.
Сирі дані: `tmp/logcat_dump.txt` (буфери main+system+events, 07-06 23:39 → 07-10 17:39, ~4 доби), `tmp/pkg_dump.txt`.

> PII/фінансові дані у релевантних рядках ftband відсутні (тільки audit/socket-inode). Сторонні гмейли з `notification_enqueue` у звіт не включені.

---

## TL;DR

- Вікно авторизації, яке «блокує все» — це **вбудований app-lock monobank** (екран `com.ftband.app.auth.flow.AuthNewActivity` + біометрія-підтвердження, `owner=com.ftband.mono`). Це **локальний PIN/відбиток, а не серверний логаут** з SMS/перереєстрацією.
- У захопленому вікні app-lock зʼявився **рівно один раз — одразу після холодного старту процесу** (перший запуск за день). За наступні ~80 хв безперервної гри повторної авторизації НЕ було, попри те що MIUI постійно «присипляв» застосунок.
- Отже перелогін корелює з **тим, що процес не був живий → холодний старт**, а НЕ з протуханням серверної сесії посеред роботи.
- Причини «процес не живий» тут дві: (а) **оновлення застосунку** (система зробила `Force stopping ... installPackageLI` 07-07) і (б) **агресивний MIItU-менеджмент памʼяті** (застосунок НЕ у whitelist Doze, SmartPower ганяє його в `hibernation` adj 900). Прямого LMK-kill саме monobank у вікні НЕ спіймано — тому драйвер «MIUI вбиває фон» треба підтвердити моніторингом.
- Впевненість: «перелогін = app-lock на холодному старті» — **висока**; «холодний старт спричинений вбивством процесу MIUI, а не серверним TTL» — **середньо-висока**.

---

## Факти пакета (`dumpsys package com.ftband.mono`)

- `versionName=8.17.0`, `versionCode=11755`, `targetSdk=36`, `minSdk=32`, `primaryCpuAbi=arm64-v8a`.
- `firstInstallTime=2022-08-23`, **`lastUpdateTime=2026-07-07 23:14:55`**, `installerPackageName=com.android.vending` (Play Store).
- `flags=[ HAS_CODE ALLOW_CLEAR_USER_DATA LARGE_HEAP ]`.
- Дозволи (релевантне): `USE_BIOMETRIC`, `USE_FINGERPRINT`, `RECEIVE_BOOT_COMPLETED`, `WAKE_LOCK`, `FOREGROUND_SERVICE`, `INTERNET`, `c2dm.RECEIVE` (FCM).
- **Немає `GET_ACCOUNTS`/`AUTHENTICATE_ACCOUNTS`** → застосунок керує сесією/токенами сам (свій auth-flow), а не через системний Android AccountManager. Логувати токени в logcat нема звідки — реліз-збірка нічого корисного не пише (тег `bank.com.ua/...` дає лише SELinux `avc: denied`, не бізнес-логіку).

## Хронологія auth (logcat events + `dumpsys usagestats`)

`usagestats` — надійне машиночитане джерело (переживає смерть процесу). Послідовність холодного старту 07-10:

```
16:17:29  am_proc_start ... com.ftband.mono  next-top-activity {.../StartActivity}   ← холодний старт (єдиний за 4 доби)
16:17:29  ACTIVITY_RESUMED  class=com.ftband.app.StartActivity     (STANDBY_BUCKET_CHANGED bucket=10 ACTIVE, reason=u-mf)
16:17:33  ACTIVITY_RESUMED  class=com.ftband.app.auth.flow.AuthNewActivity   ← ЕКРАН АВТОРИЗАЦІЇ
16:17:48  ACTIVITY_RESUMED  class=com.ftband.app.StartActivity     (auth пройдено, ~15 c на екрані)
16:17:52  BiometricScheduler [Started] owner=com.ftband.mono, requestId=7   ← застосунок сам просить відбиток
16:17:58  BaseClientMonitor ... owner=com.ftband.mono ... success: true      ← відбиток успішний
16:18:10  ACTIVITY_RESUMED  class=com.ftband.monogame.Game2028Activity       ← гра 2048
```

Далі 16:18 → 17:39 (~80 хв) — `Game2028Activity` багаторазово `RESUMED`, **жодного `AuthNewActivity`**. `usagestats` за все вікно (07-09 18:47 → 07-10 17:39) містить **тільки один** `AuthNewActivity RESUMED` (16:17:33).

Примітка: біометрія о 16:17:19 (`owner=com.android.systemui`, face/fingerprint) — це розблокування **екрана телефона**, не застосунку. App-lock monobank — це окремий `owner=com.ftband.mono` о 16:17:52.

## Життєвий цикл процесу / вбивства

- **Єдиний** `am_proc_start` для monobank у вікні — 16:17:29 07-10.
- Force-stop лише через оновлення APK:
  ```
  07-07 23:14:55  ActivityManager: Force stopping com.ftband.mono ... installPackageLI
  07-07 23:14:55  PackageManager: Update package com.ftband.mono code path ... Retain data and using new
  07-07 23:14:59  ActivityManager: Force stopping com.ftband.mono ... pkg removed   (заміна APK)
  ```
- **Жодного** `am_kill` / `am_proc_died` / `killinfo` для uid 10275 (monobank) за всі 4 доби. Для порівняння — LMK у ці ж хвилини вбивав інші застосунки (`killinfo` для shazam, novaposhtaa, globalbrowser тощо), monobank серед них немає.
- MIUI SmartPower/ProcessMemoryCleaner багато разів переводив monobank у `state=hibernation adj=700..900`, але завжди `isKilled=false` (застосунок стискали в памʼяті, не вбивали).
- Зараз (`dumpsys activity processes`): процес **живий**, `PID 22752 ProcessRecord ... com.ftband.mono/u0a275`, `oom cur=0` (був на передньому плані під час зняття).

## MIUI / фонові обмеження

- `dumpsys deviceidle`: у whitelist (`mState=ACTIVE`) monobank **відсутній** → підпадає під Doze/App Standby, коли екран вимкнено й пристрій на батареї.
- `cmd appops get com.ftband.mono RUN_ANY_IN_BACKGROUND` → `allow`; `RUN_IN_BACKGROUND` → default `allow`.
- `usagestats` standby bucket після запуску — `10` (ACTIVE); у фоні MIUI знижуватиме bucket → фриз.
- `dumpsys battery`: `level: 23`, `USB powered: true` (це наш adb-кабель).

---

## Найімовірніша причина + впевненість

1. **Перелогін = app-lock (PIN/біометрика) на ХОЛОДНОМУ старті.** Впевненість — **висока.**
   Обґрунтування: `AuthNewActivity` зʼявляється синхронно з `am_proc_start`/`StartActivity` і жодного разу — під час безперервної 80-хв сесії. Це поведінка локального замка застосунку при першому відкритті мертвого процесу, а не серверного розлогінення.

2. **Серверний TTL сесії/протухання токена — малоймовірно як основна причина.** Впевненість у спростуванні — **середня.**
   За 80 хв безперервної роботи re-auth не було; якби причиною був короткий серверний TTL, ми б побачили `AuthNewActivity` посеред сесії при тому самому PID. Повне спростування неможливе: TLS ховає мережеві 401, реліз-збірка нічого не логує, і в буфер потрапив лише один перелогін.

3. **Драйвер холодних стартів:** підтверджено — оновлення застосунку (07-07 force-stop). Ймовірно, але прямо НЕ спіймано в цьому вікні — вбивство закешованого процесу MIUI/LMK, коли бот лишає застосунок у фоні / екран гасне на батареї (не в whitelist Doze, SmartPower у hibernation). Впевненість — **середня**, треба підтвердити моніторингом.

Важлива дистинкція для бота: спостерігали лише **app-lock** (`AuthNewActivity`, локальний PIN/відбиток). **Повної перереєстрації** (телефон + SMS-OTP) у логах не було — це рідший, серверний сценарій; якщо він трапиться, класифікувати його треба окремо.

---

## Як бот детектить перелогін (доповнення до ScreenState-класифікатора)

Машиночитані ознаки, надійніші за скріншот-класифікацію:

- **Головна ознака (переживає смерть процесу):** `adb shell dumpsys usagestats` → рядок
  `type=ACTIVITY_RESUMED ... class=com.ftband.app.auth.flow.AuthNewActivity`.
  Також `taskRootClass=...AuthNewActivity` у сусідніх подіях.
- **Поточний екран:** `adb shell dumpsys activity activities | grep -i mResumedActivity` → якщо містить `AuthNewActivity` — бот зараз на екрані авторизації; якщо `Game2028Activity` — у грі; `StartActivity` — сплеш/перехід.
- **Холодний старт (контекст перелогіну):** у `logcat -d -b events`: `am_proc_start ... com.ftband.mono`; або зміна PID процесу в `dumpsys activity processes | grep ftband`.
- **Крок біометрії app-lock:** у logcat `BiometricScheduler`/`AuthenticationClient` з `owner=com.ftband.mono`.
- **Розрізнення причини:** якщо `AuthNewActivity` йде ПІСЛЯ `am_kill`/`killinfo`/`am_proc_died` (uid 10275) або зміни PID → холодний старт після смерті процесу (MIUI/оновлення). Якщо `AuthNewActivity` при ТОМУ Ж PID без смерті процесу → підозра на серверну інвалідацію сесії.

## План моніторингу, щоб зловити наступний перелогін

Ціль — зафіксувати ланцюг «смерть процесу → холодний старт → AuthNewActivity» або спростувати його (auth при тому ж PID). Усе read-only; постійний стрім НЕ запускати — робити періодичні знімки (напр., раз на 30-60 с) у датований файл:

1. Події ЖЦ і вбивств (короткий буфер, тому дампити часто):
   `adb -s <serial> logcat -d -b events -b system -v time | grep -E "am_proc_start.*ftband|am_kill|am_proc_died|killinfo|Force stopping com.ftband|ActivityManager.*ftband"`
2. Стабільний слід активностей (переживає перезавантаження/смерть процесу):
   `adb -s <serial> shell dumpsys usagestats | grep -E "com.ftband.mono" | grep -E "AuthNewActivity|Game2028Activity|StartActivity|STANDBY_BUCKET_CHANGED"`
3. Живучість/adj процесу і чи змінився PID:
   `adb -s <serial> shell dumpsys activity processes | grep -A2 -i ftband` (стежити за `PID`, `oom adj`, `hibernation`).
4. Контекст оточення на момент події: `dumpsys deviceidle` (mState, whitelist), `dumpsys battery` (screen/charging), час екрана.

Правила кореляції у зібраних знімках:
- `am_kill`/`killinfo`/`am_proc_died` для uid 10275, а згодом `am_proc_start` + `AuthNewActivity` → **підтверджує** гіпотезу «MIUI вбиває фон → холодний перелогін». Рекомендація: додати monobank у whitelist економії батареї / автозапуск MIUI (робить користувач вручну — не автоматизувати з ПК).
- `AuthNewActivity` при незмінному PID (без kill/died) → **серверна** інвалідація сесії/TTL; тоді копати мережевий бік/поведінку токена.
- Оскільки ring-буфер logcat короткий і зашумлений system_server, головним джерелом тримати `usagestats` (persist), а logcat-дамп — як тригер kill-подій.

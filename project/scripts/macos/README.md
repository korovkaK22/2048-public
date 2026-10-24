# scripts/macos — Mac mini як live-хост

Повний runbook — [`docs/macos-setup.md`](../../../docs/macos-setup.md). Тут лише що є в теці.

| файл | що робить | запуск |
|---|---|---|
| `setup.sh` | ідемпотентний бутстрап хоста: brew-пакети (`android-platform-tools` cask, `scrcpy`, `ffmpeg`, `go`, `python@3.12`), `project/.venv` + `pip install -e ".[dev,device]"`, нативна збірка обох Go-рушіїв (`go build -pgo=auto`), `fetch_weights.sh`; наприкінці **друкує** (не виконує) команди `pmset`/adb-ключ/launchd | `scripts/macos/setup.sh [--no-brew] [--no-weights] [--uv]` (з `project/`) |
| `install.sh` | рендерить шаблони plist (`@REPO@`, `@HOME@`) у `~/Library/LaunchAgents/` і робить `launchctl bootstrap` + `enable`. Вотчер стартує одразу; раннер лише завантажується — стартувати руками. `--render DIR` — тільки рендер (працює на будь-якій ОС, це тест) | `scripts/macos/install.sh` · `--uninstall` · `--render /tmp/x` |
| `com.bot2048.live.plist.template` | LaunchAgent раннера: `/bin/bash scripts/live.sh play <args>`, `ProcessType=Interactive`, **без** `KeepAlive`, `RunAtLoad=false`, `ExitTimeOut=30`. CLI-аргументи прод-команди — у масиві `ProgramArguments`, правити там | — |
| `com.bot2048.watch.plist.template` | LaunchAgent вотчера Telegram: `scripts/watch.sh`, `KeepAlive=true`, `ProcessType=Background`, `ThrottleInterval=30` | — |

## Керування

```bash
launchctl kickstart -k gui/$UID/com.bot2048.live    # старт / рестарт раннера
launchctl kill TERM  gui/$UID/com.bot2048.live      # коректний стоп -> exit_reason='killed'
launchctl print      gui/$UID/com.bot2048.live | head -40
launchctl print      gui/$UID/com.bot2048.watch | head -40
tail -f logs/launchd-live.err.log logs/launchd-watch.err.log
```

Чому раннер без `KeepAlive`: автоперезапуск після FATAL свайпав би по зламаному стану екрана;
єдиний дозволений авторестарт — бюджет у `runner/lifecycle.py` (6/год, cooldown 60 с).
Чому `Interactive`: на macOS нема CPU-affinity, `ProcessType` — єдиний важіль тримати
раннер на P-ядрах; решта фонових задач — `taskpolicy -c utility` (`docs/macos-setup.md`).

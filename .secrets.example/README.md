# Secrets setup (Mac / any fresh clone)

`.secrets/` is gitignored and never committed. Recreate it from these examples:

```sh
mkdir -p .secrets
cp .secrets.example/telegram.env.example .secrets/telegram.env
cp .secrets.example/device.env.example  .secrets/device.env
# then edit both with real values
```

- **telegram.env** — `TG_BOT_TOKEN` / `TG_CHAT_ID` / `TG_USER_HANDLE` for the alert bridge.
- **device.env** — `MONO_PIN`, only for opt-in `--auto-login` (default: auth wall = human unlocks).

The root `.env.example` documents the same variables in one place.

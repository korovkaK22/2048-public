# Docker images

Two images live in this repo. Docker is used **only** for batch work and tests
(never live/ADB — USB is not forwarded into containers on Windows).

| Image | Dockerfile | Context | Entrypoint | Purpose |
|-------|-----------|---------|------------|---------|
| **test** (multi-arch) | [`../Dockerfile`](../Dockerfile) | `project/` | `pytest` | Run the full suite identically on the x86-64 and ARM boxes |
| sim | [`Dockerfile`](./Dockerfile) | `project/` | `bot2048` | Batch simulations (`bot2048 sim ...`) — pre-existing, unchanged |

---

## Multi-arch test image (`../Dockerfile`)

One Dockerfile that builds **natively** on both machines — the target
architecture is taken from the build platform, never hardcoded. Build on each
host and you get a native image (amd64 on the i7, arm64 on the 20-core box).

Contents:
1. **stage `engine`** — compiles the Go engine (`tools/engine2048/*.go`, Go 1.24,
   `CGO_ENABLED=0`) with `GOOS/GOARCH` from the platform build args. Pure-Go +
   CGO-off, so `buildx --platform` cross-builds stay QEMU-free for the compile.
2. **stage `test`** — `python:3.12-slim`, installs `bot2048` with the `dev`
   (pytest/xdist/hypothesis) and `device` (cv2/adbutils/pillow) extras, bakes in
   the engine binary, and points `BOT2048_ENGINE2048_BIN` at it.
3. Entrypoint `pytest` — `-n auto --dist loadgroup` already comes from
   `pyproject.toml [tool.pytest.ini_options] addopts`.

### Run the tests

From the `project/` directory, on **either** machine (same command):

```bash
scripts/docker-test.sh                 # build native image + run full suite
# or manually:
docker build -t bot2048-test .
docker run --rm bot2048-test
```

Everything after the image name is appended to `pytest`:

```bash
docker run --rm bot2048-test -n 4          # pin xdist workers (overrides -n auto)
docker run --rm bot2048-test tests/core    # scope to a subtree
scripts/docker-test.sh -n 8 tests/ai       # same, via the helper
```

> **Worker count.** The image defaults to `pytest -n auto`, i.e. one worker per
> logical CPU. On the 20-core ARM box that is fine; if a heavily loaded machine
> makes timing-sensitive tests flake under `-n auto`, pin the workers explicitly
> with `-n <N>` (e.g. `-n 8`) — no rebuild needed.

### Build for the other arch without that machine (verification only)

The engine stage is not amd64-locked. To prove it cross-compiles for arm64 from
the x86 host (build stage only, no QEMU for the Go compile):

```bash
docker buildx build --platform linux/arm64 --target engine \
    --output type=tar,dest=arm.tar .
# the extracted out/engine2048 is an AArch64 ELF (e_machine 0xb7)
```

For a real arm64 **run**, build on the ARM box itself (`docker build -t
bot2048-test .`) — native, no emulation.

### Notes / gotchas

- **Build context is `project/`**, governed by [`../.dockerignore`](../.dockerignore).
  It keeps `tools/engine2048/*.go` (needed by the engine stage) but drops the
  heavy host-only tool binaries (`tools/ffmpeg`, `tools/scrcpy`,
  `tools/platform-tools`), `.venv/`, `tmp/`, `data/`, `logs/`, `.secrets/`, and
  `ondevice-apk/**/build`.
- **`COLUMNS=200`** is set in the image so headless `rich` (default 80 cols)
  doesn't truncate the CLI tables that some `sim bench` tests assert on.
- **`GOMAXPROCS=1`** is set as the batch default (see
  `docs/research/arm_20core_scaling.md`); the suite spawns its own engine.
- Verified on x86-64: **531 passed, 17 skipped** with the default `-n auto`.

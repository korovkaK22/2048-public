# syntax=docker/dockerfile:1
#
# bot2048 -- multi-arch TEST image (amd64 + arm64).
#
# PURPOSE: run the full pytest suite (Go engine + Python) identically on the
# x86-64 i7 box and the 20-core ARM box. Build NATIVELY on each machine:
#
#   docker build -t bot2048-test .          # from the project/ dir
#   docker run --rm bot2048-test            # -> pytest (green)
#
# Architecture is taken FROM THE BUILD PLATFORM, never hardcoded:
#   * plain `docker build` on each host -> BUILDPLATFORM == TARGETPLATFORM ==
#     host arch, so the engine is compiled for the host (amd64 on i7, arm64 on
#     the ARM box). No QEMU, no cross-compile.
#   * `docker buildx build --platform linux/arm64 .` from any host also works:
#     the engine stage cross-compiles (pure-Go, CGO off) to arm64 without QEMU.
#
# Live/ADB play is NEVER run here (USB is not forwarded into containers on
# Windows); this image is batch/test only.

# ---------------------------------------------------------------------------
# stage 1: build the Go engine for the TARGET arch (pure-Go, static, CGO off)
# ---------------------------------------------------------------------------
# Pin the builder to the BUILD platform and cross-compile via GOOS/GOARCH so
# that `buildx --platform` cross-builds stay fast (no QEMU for `go build`).
# On a native `docker build`, BUILDPLATFORM == TARGETPLATFORM, so this is just
# a normal native build.
FROM --platform=$BUILDPLATFORM golang:1.24-bookworm AS engine
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
# Only the engine sources -- the module has zero third-party deps (stdlib only),
# so there is no `go mod download` step.
COPY tools/engine2048/go.mod ./
COPY tools/engine2048/*.go ./
# GOOS/GOARCH come from the platform build args -> native for the target image.
# CGO_ENABLED=0 -> a fully static binary that runs on python:3.12-slim as-is.
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
        go build -trimpath -ldflags="-s -w" -o /out/engine2048 . \
    && echo "built engine2048 for ${TARGETOS}/${TARGETARCH}"

# ---------------------------------------------------------------------------
# stage 2: Python 3.12 test runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS test
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Batch default (pool findings): one OS thread per engine process; the
    # test suite spawns its own engine, so keep it single-threaded here too.
    GOMAXPROCS=1 \
    # Point the engine resolver (ai/go_engine.py) straight at the baked-in
    # binary so no `go build` is ever attempted at runtime (no Go in this stage).
    BOT2048_ENGINE2048_BIN=/app/tools/engine2048/engine2048

# Install the package + dev (test) and device (cv2/adbutils/pillow) extras so
# the WHOLE suite is collectable and green: device/vision unit tests use
# synthetic fakes (no real phone), but still import cv2/adbutils lazily.
# Cache the dependency layer by copying only what `pip install` needs first.
COPY pyproject.toml ./
COPY src ./src
RUN pip install ".[dev,device]"

# Bake in the natively-built engine and expose it at the default resolver path.
COPY --from=engine /out/engine2048 /app/tools/engine2048/engine2048
# Smoke-check the binary actually runs on this arch (ping -> pong).
RUN printf 'ping\n' | /app/tools/engine2048/engine2048 | grep -q pong \
    && echo "engine2048 smoke ok"

# Headless containers have no TTY, so `rich` falls back to an 80-column width
# and truncates the CLI tables that some `bot2048 sim bench` tests assert on
# (e.g. "expectimax(depth=1)"). Give rich a wide width so those tables render
# in full, matching a normal developer terminal.
ENV COLUMNS=200

# Test sources (incl. tests/conftest.py). pytest config lives in pyproject.toml
# (addopts already carry `-n auto --dist loadgroup`).
COPY tests ./tests

# Default: run the full suite. Extra args after the image name are appended to
# pytest (e.g. `docker run --rm bot2048-test -n 4` to pin xdist workers, or
# `docker run --rm bot2048-test tests/core` to scope the run).
ENTRYPOINT ["pytest"]
CMD []

# bot2048 simulation image.
#
# PURPOSE: batch simulations ONLY (`bot2048 sim ...`). Live/ADB play ALWAYS
# runs on the host -- USB is not forwarded into containers on Windows, so
# the [device] extra (adbutils/opencv/pillow) is deliberately NOT installed.
#
# Build (from the project/ directory):
#   docker build -f docker/Dockerfile -t bot2048 .
# Or just use docker/compose.yaml (see comments there).

FROM python:3.12-slim

WORKDIR /app

# Copy only what the package build needs (keeps the build context cacheable).
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# SQLite telemetry lives under /app/data -- mount it from the host
# (compose.yaml mounts ../data) so games survive the container.
VOLUME ["/app/data"]

ENTRYPOINT ["bot2048"]
CMD ["--help"]

#!/usr/bin/env bash
# Build the multi-arch test image NATIVELY for this host and run the suite.
#
# Same command works on the x86-64 i7 box and the 20-core ARM box -- Docker
# picks the host architecture automatically (nothing is hardcoded).
#
# Usage (from the project/ directory, or anywhere -- paths are resolved):
#   scripts/docker-test.sh                 # build + full suite (pytest -n auto)
#   scripts/docker-test.sh tests/core      # scope to a subtree
#   scripts/docker-test.sh -n 4            # pin xdist workers (overrides -n auto)
#   IMAGE=bot2048-test:dev scripts/docker-test.sh   # custom image tag
#
# Everything after the script name is appended to `pytest` inside the container.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # -> project/
image="${IMAGE:-bot2048-test:latest}"

echo ">> building ${image} (native $(uname -m)) from ${here}"
docker build -t "${image}" "${here}"

echo ">> running: pytest ${*:-<default: -n auto --dist loadgroup>}"
exec docker run --rm "${image}" "$@"

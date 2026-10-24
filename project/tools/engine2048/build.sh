#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

out=engine2048
case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) out=engine2048.exe ;;
esac

go build -o "$out" .
echo "built $out"

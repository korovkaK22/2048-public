#!/usr/bin/env bash
# Нова гілка + git-worktree у worktrees/<branch> зі спільним venv головної копії.
#
#   project/scripts/dev/new-worktree.sh feat/vision-speedup        # від main
#   project/scripts/dev/new-worktree.sh fix/x some-base-branch
#
# Прибрати після мержу:  git worktree remove worktrees/<dir> && git branch -d <branch>
set -euo pipefail

branch="${1:?usage: new-worktree.sh <branch> [base=main]}"
base="${2:-main}"

# Корінь ГОЛОВНОЇ копії (працює і якщо скрипт запущено з іншого worktree).
root="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)"
dir="$root/worktrees/${branch//\//-}"

git -C "$root" worktree add -b "$branch" "$dir" "$base"

# venv не копіюємо (~сотні МБ) — junction на головний. Тести/mypy беруть src
# поточної копії (pyproject: pythonpath = ["src", "."], mypy_path = "src").
main_venv="$root/project/.venv"
if [[ -d "$main_venv" ]]; then
    if command -v cygpath >/dev/null 2>&1; then
        MSYS2_ARG_CONV_EXCL="*" cmd /c mklink /J "$(cygpath -w "$dir/project/.venv")" "$(cygpath -w "$main_venv")" >/dev/null
    else
        ln -s "$main_venv" "$dir/project/.venv"
    fi
fi

# Підказки з інтерпретатором цієї ОС (Windows: Scripts/, POSIX: bin/).
py="./.venv/bin/python"
[[ -x "$dir/project/.venv/Scripts/python.exe" ]] && py="./.venv/Scripts/python"

cat <<EOF
worktree: $dir  (branch $branch from $base)
  cd "$dir/project"
  $py -m pytest
  PYTHONPATH=src $py -m bot2048.cli --help   # НЕ ./.venv/*/bot2048 (editable головної копії)
EOF

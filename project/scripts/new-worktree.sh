#!/usr/bin/env bash
# Нова гілка + git-worktree у worktrees/<branch> зі спільним venv головної копії.
#
#   project/scripts/new-worktree.sh feat/vision-speedup        # від main
#   project/scripts/new-worktree.sh fix/x some-base-branch
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

cat <<EOF
worktree: $dir  (branch $branch from $base)
  cd "$dir/project"
  ./.venv/Scripts/python -m pytest
  PYTHONPATH=src ./.venv/Scripts/python -m bot2048 --help   # НЕ ./.venv/Scripts/bot2048
EOF

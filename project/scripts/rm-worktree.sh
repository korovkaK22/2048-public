#!/usr/bin/env bash
# Прибрати worktree, створений new-worktree.sh (гілку лишає — видаляй сам після мержу).
#
#   project/scripts/rm-worktree.sh feat/vision-speedup
#
# Спершу знімаємо venv-junction ОКРЕМО: рекурсивне видалення теки з junction
# усередині може пройти крізь нього і знести головний venv.
set -euo pipefail

branch="${1:?usage: rm-worktree.sh <branch>}"
root="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)"
dir="$root/worktrees/${branch//\//-}"
link="$dir/project/.venv"

if [[ -e "$link" || -L "$link" ]]; then
    if command -v cygpath >/dev/null 2>&1; then
        MSYS2_ARG_CONV_EXCL="*" cmd /c rmdir "$(cygpath -w "$link")"   # rmdir junction = тільки посилання
    else
        rm "$link"
    fi
fi

git -C "$root" worktree remove "$dir"
echo "removed $dir (branch $branch kept)"

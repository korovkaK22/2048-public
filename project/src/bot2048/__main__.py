"""``python -m bot2048`` — той самий CLI, що й ``bot2048``.

Потрібен у git-worktree: скрипт ``bot2048`` у спільному venv встановлений
editable з ГОЛОВНОЇ копії і запускає її код. ``python -m bot2048`` з
``PYTHONPATH=src`` бере код поточної копії.
"""

from bot2048.cli import app

app(prog_name="bot2048")

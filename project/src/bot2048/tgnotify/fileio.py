"""Append-only JSONL writer + atomic text replace (same discipline as alerts.jsonl).

Shared by the outbox / sent-journal (``_append_jsonl``), the cursor files and
the env-file rewrite in bootstrap (``_atomic_write_text``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

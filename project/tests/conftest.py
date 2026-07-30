"""Top-level pytest config: xdist scheduling only (no fixtures).

Heavy ffmpeg/subprocess/socket/timed tests starve each other for CPU when
pytest-xdist spawns them on many workers at once (research/test_speed.md §3):
8 parallel ffmpeg pipelines miss ``first_frame_timeout`` and the whole run
gets SLOWER than serial. Pin them to ONE xdist worker via a shared
``xdist_group`` (honoured by ``--dist loadgroup``); everything else spreads
across the remaining workers.

Centralised here ON PURPOSE: zero edits inside the per-file test modules
(they are owned by other agents).
"""

from __future__ import annotations

import pytest

# Path substrings (nodeid, "/"-normalised) that must run on a single worker:
# - device/test_scrcpy + device/test_minicap: real ffmpeg decode pipelines
#   (CPU storm under -n auto — research/test_speed/xdist_auto.txt);
# - tgnotify/: real ThreadingHTTPServer + timing-sensitive reminder logic;
# - core/test_perf: intentional timed benchmark (perf_counter floor) — must
#   not compete for CPU with the parallel Python mass.
_SERIAL_PATH_SUBSTR = (
    "device/test_scrcpy",
    "device/test_minicap",
    "tgnotify/",
    "core/test_perf",
)


# tryfirst is LOAD-BEARING: inside each xdist worker the WorkerInteractor
# plugin is registered AFTER conftests, and pluggy runs hook impls in LIFO
# registration order -- so xdist's own pytest_collection_modifyitems (which
# bakes the "@<group>" suffix into nodeids for --dist loadgroup) would run
# BEFORE this hook, silently ignoring markers added here and degrading the
# whole run to plain "load" distribution (concurrent ffmpeg storm).
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        nodeid = item.nodeid.replace("\\", "/")
        if any(sub in nodeid for sub in _SERIAL_PATH_SUBSTR):
            item.add_marker(pytest.mark.xdist_group("serial_heavy"))

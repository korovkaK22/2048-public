"""`experiment_merge.py` must refuse to pool shards that do not describe ONE run.

Every check here guards a way the merged aggregates could silently misdescribe
the experiment they claim to summarise: a missing shard (two thirds of the
games reported as the whole), overlapping shards (double-counted games, and a
result that depends on argument order), or shards run with a different engine
build / weight profile.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
MERGE = PROJECT / "scripts" / "sim" / "experiment_merge.py"

ENGINE = {
    "binary": "/tmp/engine2048",
    "build_sha256": "a" * 64,
    "source_sha256": "c" * 64,
    "protocol": "1",
    "BOT2048_FARM_EMPTY_W": "340",
    "BOT2048_FARM_HOARD_TRIGGER": "99",
}


def _write_run(
    root: Path,
    name: str,
    seeds: list[int],
    *,
    count: int = 6,
    engine: dict[str, str] | None = None,
    label: str = "unit",
    max_moves: int = 6000,
) -> Path:
    """A minimal but structurally real run folder (config + results + perf)."""
    run = root / name
    run.mkdir(parents=True)
    cfg = {
        "label": label,
        "depth": 6,
        "adaptive": False,
        "stop_tile": 2048,
        "max_moves": max_moves,
        "seeds": {"base": 1000, "count": count},
        "weights": {"BOT2048_FARM_EMPTY_W": 340},
        "engine": engine if engine is not None else ENGINE,
    }
    (run / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    (run / "perf.json").write_text(json.dumps({"machine": name, "wall_s": 1.0}), encoding="utf-8")
    with (run / "results.jsonl").open("w", encoding="utf-8") as fh:
        for seed in seeds:
            fh.write(
                json.dumps(
                    {
                        "seed": seed,
                        "score": 10_000 + seed,
                        "moves": 100,
                        "max_tile": 1024,
                        "peak_1024": 1,
                        "end_reason": "no_moves",
                        "forced_2048": False,
                    }
                )
                + "\n"
            )
    return run


def _merge(
    *runs: Path, out: Path, extra: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MERGE), *(str(r) for r in runs), "--out", str(out), *(extra or [])],
        cwd=PROJECT,
        capture_output=True,
        text=True,
    )


def test_full_coverage_merges(tmp_path: Path) -> None:
    a = _write_run(tmp_path, "a", [1000, 1001, 1002])
    b = _write_run(tmp_path, "b", [1003, 1004, 1005])
    out = tmp_path / "out"

    res = _merge(a, b, out=out)

    assert res.returncode == 0, res.stderr
    merged = next(out.iterdir())
    records = [json.loads(x) for x in (merged / "results.jsonl").read_text().splitlines()]
    assert [r["seed"] for r in records] == [1000, 1001, 1002, 1003, 1004, 1005]


def test_merged_folder_carries_a_config_and_can_be_re_merged(tmp_path: Path) -> None:
    """The cross-machine flow: pool one machine's shards, then pool THAT with
    the other machine's. Without a config.json in the merged folder the second
    step hits the 'cannot verify parameters' refusal — a dead end."""
    a = _write_run(tmp_path, "a", [1000, 1001, 1002])
    b = _write_run(tmp_path, "b", [1003, 1004])
    c = _write_run(tmp_path, "c", [1005])
    out = tmp_path / "out"

    first = _merge(a, b, out=out, extra=["--allow-partial"])
    assert first.returncode == 0, first.stderr
    partial = next(out.iterdir())
    cfg = json.loads((partial / "config.json").read_text())
    assert cfg["partial"] is True
    assert cfg["engine"]["build_sha256"] == ENGINE["build_sha256"]

    second = _merge(partial, c, out=tmp_path / "out2")
    assert second.returncode == 0, second.stderr
    final = next((tmp_path / "out2").iterdir())
    assert json.loads((final / "config.json").read_text())["partial"] is False
    assert len((final / "results.jsonl").read_text().strip().splitlines()) == 6


def test_missing_shard_fails(tmp_path: Path) -> None:
    a = _write_run(tmp_path, "a", [1000, 1001])
    b = _write_run(tmp_path, "b", [1002, 1003])  # 1004, 1005 never ran

    res = _merge(a, b, out=tmp_path / "out")

    assert res.returncode == 2
    assert "coverage is incomplete" in res.stderr
    assert "1004" in res.stderr and "1005" in res.stderr


def test_overlapping_seeds_fail(tmp_path: Path) -> None:
    a = _write_run(tmp_path, "a", [1000, 1001, 1002, 1003])
    b = _write_run(tmp_path, "b", [1003, 1004, 1005])  # 1003 in both

    res = _merge(a, b, out=tmp_path / "out")

    assert res.returncode == 2
    assert "OVERLAP" in res.stderr
    assert "1003" in res.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("label", "benchmark"),  # same weights/depth as baseline — a DIFFERENT run
        ("max_moves", 2000),  # truncated games score lower for an unrelated reason
    ],
)
def test_differing_run_parameters_fail(tmp_path: Path, field: str, value: object) -> None:
    a = _write_run(tmp_path, "a", [1000, 1001, 1002])
    b = _write_run(tmp_path, "b", [1003, 1004, 1005], **{field: value})  # type: ignore[arg-type]

    res = _merge(a, b, out=tmp_path / "out")

    assert res.returncode == 2
    assert "DIFFERENT parameters" in res.stderr


def test_differing_engine_sources_fail(tmp_path: Path) -> None:
    """Same config text, different engine code: the 5 weights a config used to
    leave implicit come from the build's compiled-in defaults, so a different
    engine is a different heuristic (HOARD_TRIGGER 2 -> 99 was exactly this,
    and it changed nothing in any config)."""
    a = _write_run(tmp_path, "a", [1000, 1001, 1002])
    b = _write_run(
        tmp_path,
        "b",
        [1003, 1004, 1005],
        engine={**ENGINE, "source_sha256": "d" * 64, "BOT2048_FARM_HOARD_TRIGGER": "2"},
    )

    res = _merge(a, b, out=tmp_path / "out")

    assert res.returncode == 2
    assert "DIFFERENT parameters" in res.stderr


def test_same_sources_different_binaries_merge(tmp_path: Path) -> None:
    """The cross-machine case: an arm64 Mac and an amd64 Windows box build the
    same commit into byte-different binaries. Those shards MUST still pool —
    signing a run by its binary hash would break the tool's whole purpose."""
    a = _write_run(tmp_path, "a", [1000, 1001, 1002])
    b = _write_run(
        tmp_path,
        "b",
        [1003, 1004, 1005],
        engine={**ENGINE, "build_sha256": "b" * 64, "binary": r"C:\engine2048.exe"},
    )

    res = _merge(a, b, out=tmp_path / "out")

    assert res.returncode == 0, res.stderr

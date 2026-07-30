#!/usr/bin/env python
"""Pool several experiment run folders into one combined summary.

Use it to merge DISJOINT seed shards of the SAME weight profile that were run
on different machines (e.g. M4 Pro ran shard 0/2, Windows ran shard 1/2), and
to sanity-check that they really share the same weights before pooling.

Reads each run's ``results.jsonl`` (+ ``config.json`` / ``perf.json``), pools
the per-game records, recomputes the aggregates with the SAME formulas as
``experiment_runner.py``, and writes a merged folder:

    research/experiments/<merged-id>/
        config.json       — the profile these shards share (so a merged folder
                            is itself mergeable — the cross-machine flow)
        results.jsonl     — all games from all shards
        aggregates.json   — combined tail metrics
        perf.json         — combined throughput (sum of games, machine list)
        summary.md        — combined human-readable summary
        sources.json      — which run folders were merged

Refuses to merge (exit 2) unless the shards agree on the FULL run signature —
engine build hash + all 13 farm weights the engine reported it applied, plus
label/depth/adaptive/stop_tile/max_moves/p4 — are disjoint in seeds, and
together cover the config's whole seed range (``--allow-partial`` to opt out
of the last one deliberately).

Usage (from project/):
    ./.venv/Scripts/python.exe scripts/experiment_merge.py \
        research/experiments/RUN_A research/experiments/RUN_B \
        [--label baseline] [--allow-partial] [--out research/experiments]

Pass either run folders or results.jsonl files; both work.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Reuse the exact aggregation + markdown formulas from the runner so a merged
# summary is directly comparable to a single-run summary.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from experiment_runner import _aggregate, _resolve_p4, _write_summary_md


def _resolve(path: Path) -> tuple[Path, Path]:
    """Return (results.jsonl, run_dir) for a folder or a jsonl file arg."""
    if path.is_dir():
        return path / "results.jsonl", path
    return path, path.parent


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _run_signature(cfg: dict[str, Any]) -> dict[str, Any]:
    """Canonical set of run parameters that must match for two shards to be
    poolable. Mirrors the effective defaults ``experiment_runner`` applies, so
    an omitted key compares equal to its default rather than looking different.

    Two runs are comparable only if they share ALL of these. The signature is
    built from what the engine REPORTED (``config.json`` -> ``engine``): the
    hash of the engine SOURCES it was built from, and the 13 farm weights it
    actually applied. Comparing the config's ``weights`` block instead would
    pool runs that merely *look* alike — a config pinning 8 weights says
    nothing about the other 5, and two builds with different compiled-in
    defaults for them produce different heuristics under a byte-identical
    config.

    The engine's *binary* hash is deliberately NOT in here: an arm64 Mac and an
    amd64 Windows box building the same commit produce different binaries, and
    pooling those two shards is the entire point of this tool. The source hash
    is what is portable, and it is what actually determines behaviour.

    ``label`` and ``max_moves`` are in here too: a 12-game ``benchmark`` shares
    depth/weights with the 200-game ``baseline`` pool but is not part of it,
    and a shard truncated at ``max_moves=2000`` scores lower for a reason that
    has nothing to do with the profile under test.
    """
    engine = cfg.get("engine") or {}
    weights = {k: v for k, v in engine.items() if k.startswith("BOT2048_FARM_")}
    return {
        "label": cfg.get("label"),
        "depth": int(cfg.get("depth", 6)),
        "adaptive": bool(cfg.get("adaptive", False)),
        "stop_tile": int(cfg.get("stop_tile", 2048)),
        "max_moves": int(cfg.get("max_moves", 6000)),
        "p4": _resolve_p4(cfg),
        "engine_source_sha256": engine.get("source_sha256"),
        # Fall back to the config's own block only for pre-provenance runs,
        # which have no engine report to trust.
        "weights": weights or cfg.get("weights", {}),
    }


def _check_seed_coverage(
    cfg: dict[str, Any], records: list[dict[str, Any]], allow_partial: bool
) -> None:
    """Refuse a pool that does not cover the config's full seed range.

    Merging shards 0/3 and 1/3 and forgetting 2/3 used to print "merged 2
    runs" and nothing else — the resulting aggregates silently describe two
    thirds of the experiment while claiming to be the experiment.

    ``allow_partial`` is the deliberate exception: pre-pooling the shards one
    machine ran, before merging that with another machine's. It is opt-in and
    stamped into the merged config, so a partial pool can never be mistaken
    for a complete one.
    """
    seeds_cfg = cfg.get("seeds") or {}
    if "base" not in seeds_cfg or "count" not in seeds_cfg:
        sys.stderr.write(
            "ERROR: config.json has no seeds.base/seeds.count — cannot verify that the "
            "merged shards cover the full seed range.\n"
        )
        raise SystemExit(2)
    base, count = int(seeds_cfg["base"]), int(seeds_cfg["count"])
    expected = set(range(base, base + count))
    got = {int(r["seed"]) for r in records}
    missing = sorted(expected - got)
    extra = sorted(got - expected)
    # A seed outside the declared range is always wrong — it means these
    # records are not from this config, and --allow-partial cannot excuse it.
    if extra:
        sys.stderr.write(
            f"ERROR: {len(extra)} seed(s) outside the config's range "
            f"[{base}..{base + count - 1}]: {_brief(extra)}\n"
            "These records did not come from this config — refusing to merge.\n"
        )
        raise SystemExit(2)
    if missing and not allow_partial:
        sys.stderr.write(
            f"ERROR: seed coverage is incomplete — expected {count} seeds "
            f"[{base}..{base + count - 1}], got {len(got)}, "
            f"missing {len(missing)}: {_brief(missing)}\n"
            "Merge every shard of the run. If you are deliberately pre-pooling one "
            "machine's shards before merging with another machine's, pass --allow-partial.\n"
        )
        raise SystemExit(2)
    if missing:
        sys.stderr.write(
            f"warning: PARTIAL pool — {len(got)}/{count} seeds "
            f"(missing {len(missing)}: {_brief(missing)}). Not the full experiment.\n"
        )


def _brief(seeds: list[int], limit: int = 12) -> str:
    head = ", ".join(str(s) for s in seeds[:limit])
    return head + (f", ... (+{len(seeds) - limit} more)" if len(seeds) > limit else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path, help="Run folders or results.jsonl files.")
    parser.add_argument("--label", default=None, help="Label for the merged run (default: config).")
    parser.add_argument("--out", type=Path, default=Path("research/experiments"))
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Permit a pool that does not cover the config's full seed range (e.g. "
        "pre-pooling one machine's shards). The merged config is stamped partial.",
    )
    args = parser.parse_args()

    all_records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    signatures: dict[str, str] = {}  # canonical run signature -> first run dir seen
    machines: list[str] = []
    total_wall = 0.0
    label = args.label
    first_cfg: dict[str, Any] = {}

    for run in args.runs:
        jsonl, run_dir = _resolve(run)
        recs = _load_jsonl(jsonl)
        cfg_path = run_dir / "config.json"
        perf_path = run_dir / "perf.json"
        # A run without a config.json cannot have its parameters verified, so it
        # cannot be safely pooled — refuse instead of merging blind.
        if not cfg_path.is_file():
            sys.stderr.write(
                f"ERROR: {run_dir} has no config.json — refusing to merge a run "
                "whose parameters (depth/p4/adaptive/stop_tile/weights) cannot be "
                "verified.\n"
            )
            raise SystemExit(2)
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        perf = json.loads(perf_path.read_text(encoding="utf-8")) if perf_path.is_file() else {}
        signatures.setdefault(json.dumps(_run_signature(cfg), sort_keys=True), str(run_dir))
        if not first_cfg:
            first_cfg = cfg
        if perf.get("machine"):
            machines.append(str(perf["machine"]))
        total_wall += float(perf.get("wall_s", 0.0))
        if label is None and cfg.get("label"):
            label = cfg["label"]
        all_records.extend(recs)
        sources.append(
            {"path": str(run_dir), "games": len(recs), "machine": perf.get("machine")}
        )

    # Safety: refuse to silently pool runs whose FULL parameter set differs.
    # Same weights but a different depth/p4/adaptive/stop_tile => non-comparable.
    if len(signatures) > 1:
        sys.stderr.write(
            "ERROR: runs have DIFFERENT parameters (depth/p4/adaptive/stop_tile/weights)"
            " — refusing to merge:\n"
            + "\n".join(f"  {path}: {sig}" for sig, path in signatures.items())
            + "\nMerge only shards of the SAME profile.\n"
        )
        raise SystemExit(2)

    # Shards must be DISJOINT. An overlap means the split was wrong, and which
    # copy of a seed survives then depends on the order of the CLI arguments —
    # a merged result that changes with argv is not a measurement. Refuse.
    seen: dict[int, dict[str, Any]] = {}
    dupes: list[int] = []
    for r in all_records:
        s = int(r["seed"])
        if s in seen:
            dupes.append(s)
            continue
        seen[s] = r
    if dupes:
        sys.stderr.write(
            f"ERROR: shards OVERLAP on {len(dupes)} seed(s): {_brief(sorted(set(dupes)))}\n"
            "Shards must be disjoint — merging them would double-count games and make the "
            "result depend on argument order. Fix the --shard split and re-run.\n"
        )
        raise SystemExit(2)
    records = sorted(seen.values(), key=lambda r: r["seed"])

    if not records:
        sys.stderr.write("ERROR: no game records to merge — all source runs were empty.\n")
        raise SystemExit(2)

    # ... and TOGETHER they must cover the whole seed range the config declares.
    _check_seed_coverage(first_cfg, records, args.allow_partial)
    seeds_cfg = first_cfg.get("seeds") or {}
    partial = len(records) < int(seeds_cfg["count"])

    label = label or "merged"
    agg = _aggregate(records)
    total_moves = agg["moves_total"]
    seed_lo = records[0]["seed"]
    seed_hi = records[-1]["seed"]

    perf = {
        "machine": "+".join(dict.fromkeys(machines)) or "merged",
        "shard": "merged",
        "workers": 0,
        "gomaxprocs": 0,
        "games": len(records),
        "wall_s": round(total_wall, 3),
        "games_per_hour": round(len(records) / total_wall * 3600, 1) if total_wall > 0 else 0.0,
        "moves_per_sec": round(total_moves / total_wall, 1) if total_wall > 0 else 0.0,
        "moves_total": total_moves,
        "ms_per_move_mean": None,
        "sources": sources,
    }

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_id = f"{label}__merged__{ts}"
    run_dir = args.out / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    (run_dir / "aggregates.json").write_text(json.dumps(agg, indent=2), encoding="utf-8")
    (run_dir / "perf.json").write_text(json.dumps(perf, indent=2), encoding="utf-8")
    (run_dir / "sources.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")

    # All shards share one signature (verified above), so the first run's config
    # is representative — reuse it verbatim for the weights/depth block.
    cfg_stub = dict(first_cfg)
    cfg_stub["label"] = label
    cfg_stub.setdefault("depth", "?")
    cfg_stub.setdefault("weights", {})
    cfg_stub["partial"] = partial
    # A merged folder is itself a run folder: write the config that describes
    # it, or the merge is a dead end (merging a merge with a third shard hits
    # the "no config.json -> refuse" check above — exactly the cross-machine
    # flow this tool exists for).
    (run_dir / "config.json").write_text(
        json.dumps(cfg_stub, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _write_summary_md(run_dir / "summary.md", cfg_stub, run_id, agg, perf, seed_lo, seed_hi)

    print(
        f"merged {len(sources)} runs -> {len(records)} games "
        f"(machines: {perf['machine']})\n"
        f"  mean {agg['score_mean']:,.0f} max {agg['score_max']:,} "
        f"P(>=48k) {agg['p_score_ge']['48000']:.1%} "
        f"3x1024 {agg['pct_reach_3x1024']:.1%} 4x1024 {agg['pct_reach_4x1024']:.1%}\n"
        f"  -> {run_dir}"
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Farm-heuristic experiment runner — one command, one reproducible run.

Reads a JSON experiment config (weights + seeds + depth), plays N farm-mode
games in parallel (one persistent Go engine per worker, GOMAXPROCS=1, the
monobank "assembling 2048 ends the game" taboo), and writes a *self-contained*
result folder under ``research/experiments/<run-id>/``:

    config.json      — verbatim snapshot of the config that produced this run
    results.jsonl    — one JSON object per game: the epitaph (score, moves,
                       tiles, end reason) AND the trajectory telemetry that says
                       WHY the game died (reservoir birth schedule, empty-cell
                       history, the (k,e) occupancy that yields the hazard
                       function, the taboo-safe choice set, the death context)
    aggregates.json  — machine-readable summary (moves mu/sigma/quartiles, the
                       fitted P(moves >= 2569) with a bootstrap CI, the pooled
                       hazard function, reservoir schedule, end reasons)
    trace.jsonl      — [--trace only] full per-ply trajectory of every game
    perf.json        — timings (wall, games/hour, moves/s, ms/move, machine)
    summary.md       — human-readable summary (headline = the TAIL: max, P>=48k)

The folder is git-trackable and zip-portable: zip it, move it to another
machine, and ``experiment_merge.py`` pools several of them (same weight
profile, disjoint seed shards) into one combined summary.

It does NOT modify anything under core/ai/sim/storage — it only *uses*
``bot2048.sim.runner.play_game`` (the exact same simulator the CLI uses) and
``bot2048.ai.go_engine.GoExpectimaxStrategy`` (farm mode), exactly like
``scripts/bench_weights.py`` drives the Python engine.

Usage (from project/):
    ./.venv/Scripts/python.exe scripts/experiment_runner.py CONFIG.json \
        [--machine m4pro|m4|windows|LABEL] [--workers N] [--shard IDX/COUNT] \
        [--engine-bin PATH] [--record-db] [--out DIR]

Prefer the thin wrapper ``scripts/experiment.sh CONFIG.json ...`` which just
picks the venv interpreter for you.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The inferential statistics (fitted normal tail + bootstrap CI) already exist,
# calibrated on this project's numbers — reuse them rather than growing a second,
# subtly different copy. scripts/ is not a package, so add it to the path first
# (this also works when the runner is loaded by path, as the tests do).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from arena import TARGET_MOVES, bootstrap_ci, norm_tail

from bot2048.sim.telemetry import hazard_table

# --------------------------------------------------------------------------
# Farm weight env-var names. MUST match the overrides read once at engine
# startup in tools/engine2048/farm.go (initFarmTables). Anything listed under
# "weights" in the config is exported to the engine subprocess via these.
# --------------------------------------------------------------------------
FARM_ENV_VARS = (
    "BOT2048_FARM_EMPTY_W",
    "BOT2048_FARM_MERGE_W",
    "BOT2048_FARM_CORNER_W",
    "BOT2048_FARM_EDGE_W",
    "BOT2048_FARM_STAIR_W",
    "BOT2048_FARM_SNAKE_W",
    "BOT2048_FARM_SNAKE_RATIO",
    "BOT2048_FARM_CORNER_RANK_W",
    "BOT2048_FARM_CONVEX_W",
    "BOT2048_FARM_LATE_EMPTY_W",
    "BOT2048_FARM_LATE_GRAD_MULT",
    "BOT2048_FARM_LATE_CORNER_RANK_W",
    "BOT2048_FARM_HOARD_TRIGGER",
    # Not a farm weight but a SEARCH-SHAPE knob (search.go, initSearchParams):
    # the cumulative-probability cutoff. It decides how deep the nominal depth-N
    # search actually reaches, so it belongs to the run's provenance exactly like
    # a weight does — and, like a weight, it must be popped when a config omits it.
    "BOT2048_CPROB_THRESH",
    "BOT2048_FARM_MOBILITY_W",
    "BOT2048_FARM_MOB_PAIR_PEN",
    "BOT2048_FARM_QUADCORNER_W",
    "BOT2048_FARM_QUAD_RATIO",
    "BOT2048_FARM_EMPTY_CURVE_W",
)


def _apply_farm_env(weights: dict[str, Any]) -> None:
    """Make the engine's environment say EXACTLY what the config says.

    Exports every weight the config pins, and — just as important — REMOVES
    every farm var it does not: an ``export BOT2048_FARM_HOARD_TRIGGER=2``
    left over in the shell would otherwise silently re-tune the heuristic of
    every run started from it, with nothing in the result folder to show for
    it. The config is the only input; the shell is not.
    """
    for key in FARM_ENV_VARS:
        value = weights.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)


# Per-machine defaults (task profile #4). Explicit --workers overrides these.
MACHINE_PROFILES: dict[str, dict[str, int]] = {
    "m4pro": {"workers": 13, "gomaxprocs": 1},  # M4 Pro: 14 cores, leave 1 for OS
    # Mac mini M4: 4P + 6E = 10 cores. 8 workers leaves the OS a core and, when
    # the live runner shares the box, run with <= 4 (or `taskpolicy -b`) instead.
    "m4": {"workers": 8, "gomaxprocs": 1},
    "windows": {"workers": max(1, (os.cpu_count() or 4) - 2), "gomaxprocs": 1},
}

# Score thresholds reported in the tail metrics. 48k is the headline (beat the
# 47k leader); the others give context for the right tail of the distribution.
SCORE_THRESHOLDS = (45_000, 48_000, 50_000)

# Presence of any of these in a game record means it carries trajectory
# telemetry. Runs recorded before it existed have none, and the blocks derived
# from them are then reported over the subset that does (``trajectory_games``).
TRAJECTORY_KEYS = frozenset(
    {"risk_visits", "reservoir_births", "empty_windows", "taboo_blocked_plies"}
)


def _make_run_id(
    label: str,
    machine: str,
    shard_index: int,
    shard_count: int,
    now: datetime | None = None,
) -> str:
    """Collision-resistant run-folder id.

    The timestamp is second-precise and ``run_dir.mkdir(exist_ok=True)`` would
    otherwise let a second run of the SAME label/machine/shard started within
    the same wall second silently overwrite the first. A short random suffix
    keeps concurrent (or rapidly retried) runs in distinct folders.
    """
    ts = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{label}__{machine}__shard{shard_index}of{shard_count}__{ts}__{uuid.uuid4().hex[:8]}"


# ==========================================================================
# Worker side (runs in a separate process — module-level so it pickles).
# ==========================================================================
def _run_chunk(payload: dict[str, Any]) -> dict[str, Any]:
    """Play a contiguous block of seeds in ONE process with ONE engine.

    Returns {"records": [per-game dicts], "traces", "engine_ms_sum", "engine_ms_n"}.
    """
    seeds: list[int] = payload["seeds"]
    cfg: dict[str, Any] = payload["cfg"]
    db_path: str | None = payload["db_path"]
    trace: bool = payload.get("trace", False)

    # Export weights + GOMAXPROCS BEFORE importing/spawning the engine: the Go
    # runtime reads GOMAXPROCS and farm.go reads the BOT2048_FARM_* vars once,
    # at engine process startup (main.go -> initTables -> initFarmTables).
    _apply_farm_env(cfg["weights"])
    os.environ["GOMAXPROCS"] = str(cfg["gomaxprocs"])

    from bot2048.ai.go_engine import GoExpectimaxStrategy
    from bot2048.core.types import SpawnModel
    from bot2048.sim.runner import play_game
    from bot2048.sim.telemetry import TelemetryRecorder

    spawn_model = SpawnModel()
    p4 = _resolve_p4(cfg)

    engine_bin = cfg.get("engine_bin")
    strat = GoExpectimaxStrategy(
        depth=cfg["depth"],
        p4=float(p4),
        adaptive=cfg["adaptive"],
        farm=True,
        binary=Path(engine_bin) if engine_bin else None,
    )

    inner = None
    if db_path is not None:
        try:
            from bot2048.storage import ExperimentMeta, SqliteRecorder

            meta = ExperimentMeta(
                experiment_id=cfg["experiment_id"],
                kind="sim",
                strategy=strat.name,
                params=cfg["db_params"],
            )
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            inner = SqliteRecorder(Path(db_path), meta)
        except Exception as exc:  # telemetry is best-effort
            sys.stderr.write(f"warning: SQLite telemetry disabled ({exc})\n")

    recorder = TelemetryRecorder(inner, trace=trace)
    stop_exponent = cfg["stop_exponent"]
    max_moves = cfg["max_moves"]
    records: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    engine_ms_sum = 0.0
    engine_ms_n = 0
    try:
        for seed in seeds:
            summary = play_game(
                strat,
                spawn_model,
                seed=seed,
                recorder=recorder,
                max_moves=max_moves,
                experiment_id=cfg["experiment_id"],
                params=cfg["db_params"],
                stop_exponent=stop_exponent,
            )
            # pop, not get: the tracker holds the game's per-ply history and a
            # worker plays many games in one process.
            g = recorder.pop(summary.game_id)
            tele = g.summary() if g is not None else {}
            engine_ms_sum += g.engine_ms_sum if g is not None else 0.0
            engine_ms_n += g.engine_ms_n if g is not None else 0
            records.append(
                {
                    "seed": seed,
                    "score": summary.final_score,
                    "moves": summary.num_moves,
                    "max_exponent": summary.max_exponent,
                    "max_tile": 2**summary.max_exponent if summary.max_exponent else 0,
                    "end_reason": summary.end_reason,
                    "forced_2048": summary.end_reason == "tile_cap",
                    "duration_s": round(summary.duration_s, 3),
                    **tele,
                }
            )
            if trace and g is not None:
                traces.append({"seed": seed, "plies": g.trace_rows()})
    finally:
        recorder.close()
        closer = getattr(strat, "close", None)
        if callable(closer):
            closer()

    return {
        "records": records,
        "traces": traces,
        "engine_ms_sum": engine_ms_sum,
        "engine_ms_n": engine_ms_n,
    }


# ==========================================================================
# Parent side: config, sharding, orchestration, aggregation, output.
# ==========================================================================
def _load_config(path: Path) -> dict[str, Any]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for required in ("label", "weights", "seeds"):
        if required not in cfg:
            raise ValueError(f"config missing required key {required!r}")
    return cfg


def _resolve_p4(cfg: dict[str, Any]) -> float:
    """The p4 the engine will actually be given (config value, else the
    simulator's own spawn model) — so a config that omits it still records
    and compares by the SAME number a run used."""
    p4 = cfg.get("p4")
    if p4 is not None:
        return float(p4)
    from bot2048.core.types import SpawnModel

    return float(dict(SpawnModel().exponent_probs).get(2, 0.0))


def _shard_seeds(base: int, count: int, shard_index: int, shard_count: int) -> list[int]:
    """Contiguous block sharding: machine `shard_index` of `shard_count` gets a
    contiguous slice of the full [base, base+count) seed range. All machines
    together cover the full range exactly once (disjoint, no overlap)."""
    if shard_count < 1 or not (0 <= shard_index < shard_count):
        raise ValueError(f"invalid shard {shard_index}/{shard_count}")
    full = list(range(base, base + count))
    per = math.ceil(count / shard_count)
    start = shard_index * per
    return full[start : start + per]


def _tasks(seeds: list[int]) -> list[list[int]]:
    """One task per SEED — the pool queue, not a static split, balances them.

    Games are not equal-cost: their length varies with a CV of ~9% (measured
    over the project's own runs: 1458..2108 moves). The old split handed each
    worker one contiguous block of ceil(N/workers) seeds up front, so the run
    could not finish before the unluckiest block did, and a block that drew
    long games left every other worker idle at the end. With 200 seeds and 13
    workers it was worse still: ceil(200/13) = 16 gives 12 blocks of 16 and one
    of 8, so the last worker did HALF the work of the others and then sat idle.

    One seed per task lets ``ProcessPoolExecutor`` hand out the next game to
    whichever worker is free. Monte-Carlo over the measured length distribution
    (200 seeds, 13 workers): makespan falls from 1.080x to 1.034x of ideal, i.e.
    ~4.3% less wall for the same games. Intermediate chunk sizes are NOT a
    middle ground — they requantise the tail and can be worse than either end
    (a 5-seed chunk gives 40 tasks over 13 workers = 1.26x ideal).

    Cost of the finer grain: one engine subprocess per game instead of per
    block. Measured spawn + table init = 20 ms against ~35 s of CPU per game,
    i.e. 0.06%. It buys the 4.3% many times over.

    EXACT: a game is fully determined by its seed, so which worker plays it —
    and in what order — cannot change any result.
    """
    return [[seed] for seed in seeds]


def _sig(x: float, digits: int = 4) -> float:
    """Round to significant digits, not decimal places.

    The fitted tail probability is ~1e-6 for the shipped profile and orders of
    magnitude smaller for a worse arm. Rounding it to N DECIMALS collapses every
    such arm to 0.0 and destroys the only ranking we have.
    """
    if x == 0.0 or not math.isfinite(x):
        return x
    return round(x, -math.floor(math.log10(abs(x))) + (digits - 1))


def _quantile(xs: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list."""
    if not xs:
        return 0.0
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _moves_stats(moves: list[int]) -> dict[str, Any]:
    """The distribution of MOVES — the metric the objective is actually in.

    score = 18.2 x moves (r^2 = .998, docs/archive/audit-2026-07-11.md §3.3), so moves
    is the same signal with less noise, and the goal — one game >= 48k — is
    P(moves >= 2569). That is a TAIL, and a tail is a function of sigma exactly
    as much as of mu: an arm can win the mean and lose the objective. Reporting
    only ``moves_mean`` (all this file used to do) hides half of it.

    ``p_ge_target`` is the NORMAL-FITTED tail with a bootstrap 95% CI, not the
    empirical count: the target sits ~4.5 sigma out, so the empirical estimate
    is 0/n for every arm we can afford to run and ranks nothing. The empirical
    number is kept beside it, honestly labelled.
    """
    n = len(moves)
    if not n:
        return {}
    xs = sorted(float(m) for m in moves)
    mu = statistics.mean(xs)
    sd = statistics.stdev(xs) if n > 1 else 0.0
    fitted = norm_tail(mu, sd, TARGET_MOVES)
    lo, hi = (
        bootstrap_ci(
            xs,
            lambda s: norm_tail(
                statistics.mean(s), statistics.stdev(s) if len(s) > 1 else 0.0, TARGET_MOVES
            ),
        )
        if n > 1
        else (0.0, 0.0)
    )
    return {
        "moves_mean": round(mu, 1),
        "moves_stdev": round(sd, 1),
        "moves_median": round(statistics.median(xs), 1),
        "moves_q25": round(_quantile(xs, 0.25), 1),
        "moves_q75": round(_quantile(xs, 0.75), 1),
        "moves_min": int(xs[0]),
        "moves_max": int(xs[-1]),
        "moves_total": sum(moves),
        "moves_target": TARGET_MOVES,
        # z = (mu - T)/sigma. P(moves >= T) = Phi(z) is monotone in z, so THIS
        # is the ordering of the objective — not the mean.
        "moves_z": round((mu - TARGET_MOVES) / sd, 3) if sd else None,
        "p_moves_ge_target": _sig(fitted),
        "p_moves_ge_target_ci95": [_sig(lo), _sig(hi)],
        "p_moves_ge_target_empirical": round(sum(1 for m in xs if m >= TARGET_MOVES) / n, 4),
    }


def _reservoir_schedule(records: list[dict[str, Any]], denom: int) -> dict[str, Any]:
    """When the k-th 1024 is born, and how squeezed the board is when it is.

    Measured on the shipped profile: 491 / 957 / 1420 / 1895 — a step of 466 =
    1024/2.2, i.e. reservoirs arrive exactly on the mass schedule. And the
    min-empty around the birth collapses to 0.00 by k=4: the bot spends its
    whole structure buying the 4th reservoir. A profile that survives longer
    must move one of these two numbers, and this is where you see which.
    """
    out: dict[str, Any] = {}
    # ``denom`` = the games that CARRY trajectory telemetry, not len(records):
    # in a mixed old+new pool the rest cannot be said to have "not reached" k —
    # they simply never recorded it.
    n = denom
    for k in range(1, 6):
        key = str(k)
        plies = [
            r["reservoir_births"][key]
            for r in records
            if isinstance(r.get("reservoir_births"), dict) and key in r["reservoir_births"]
        ]
        mins = [
            r["birth_crisis"][key]["min_empty"]
            for r in records
            if isinstance(r.get("birth_crisis"), dict) and key in r["birth_crisis"]
        ]
        if not plies:
            continue
        out[key] = {
            "reached": len(plies),
            "reached_pct": round(len(plies) / n, 4) if n else 0.0,
            "birth_ply_mean": round(statistics.mean(plies), 1),
            "birth_ply_median": round(statistics.median(plies), 1),
            "birth_min_empty_mean": round(statistics.mean(mins), 3) if mins else None,
        }
    return out


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [r["score"] for r in records]
    n = len(scores)
    peaks = [r.get("peak_1024", 0) for r in records]
    forced = sum(1 for r in records if r["forced_2048"])
    tile_hist = Counter(r["max_tile"] for r in records)
    reason_hist = Counter(r["end_reason"] for r in records)
    agg: dict[str, Any] = {
        "games": n,
        "score_mean": round(statistics.mean(scores), 1) if n else 0,
        "score_median": round(statistics.median(scores), 1) if n else 0,
        "score_max": max(scores) if n else 0,
        "score_min": min(scores) if n else 0,
        "score_stdev": round(statistics.pstdev(scores), 1) if n > 1 else 0.0,
        "p_score_ge": {
            str(t): round(sum(1 for s in scores if s >= t) / n, 4) if n else 0.0
            for t in SCORE_THRESHOLDS
        },
        "pct_reach_2x1024": round(sum(1 for p in peaks if p >= 2) / n, 4) if n else 0.0,
        "pct_reach_3x1024": round(sum(1 for p in peaks if p >= 3) / n, 4) if n else 0.0,
        "pct_reach_4x1024": round(sum(1 for p in peaks if p >= 4) / n, 4) if n else 0.0,
        "forced_2048_rate": round(forced / n, 4) if n else 0.0,
        "max_tile_hist": {str(k): v for k, v in sorted(tile_hist.items())},
        "end_reason_hist": dict(reason_hist),
        "end_reason_pct": (
            {k: round(v / n, 4) for k, v in sorted(reason_hist.items())} if n else {}
        ),
        "moves_total": sum(r["moves"] for r in records),
    }
    agg.update(_moves_stats([r["moves"] for r in records]))

    # --- trajectory-derived blocks. Absent from pre-telemetry runs, and then
    # simply omitted: a merged pool of old + new shards stays valid, it just
    # says less. (Never fabricated — an empty dict is honest, a zero is not.)
    #
    # ``trajectory_games`` is load-bearing for exactly that case. The telemetry
    # is Python-side, so a pre-telemetry shard has the SAME engine source hash
    # as a new one and experiment_merge will happily pool them — at which point
    # the hazard table below would describe a SUBSET of the pool while sitting
    # next to ``games: 200``. This number says which subset.
    telemetry_games = sum(1 for r in records if not TRAJECTORY_KEYS.isdisjoint(r))
    if telemetry_games:
        agg["trajectory_games"] = telemetry_games
    schedule = _reservoir_schedule(records, telemetry_games)
    if schedule:
        agg["reservoir_schedule"] = schedule
    hazard = hazard_table(records)
    if hazard:
        agg["hazard_100"] = hazard
    blocked = [r["taboo_blocked_plies"] for r in records if "taboo_blocked_plies" in r]
    if blocked:
        # Denominator = the moves of the games that measured it, not of the pool.
        plies = sum(r["moves"] for r in records if "taboo_blocked_plies" in r)
        agg["taboo_blocked_plies_total"] = sum(blocked)
        agg["taboo_blocked_frac"] = round(sum(blocked) / plies, 6) if plies else 0.0
    deaths = [r["death"] for r in records if isinstance(r.get("death"), dict)]
    pre = [d["empty_at_minus20"] for d in deaths if d.get("empty_at_minus20") is not None]
    if pre:
        agg["empty_20_before_death_mean"] = round(statistics.mean(pre), 3)
        agg["empty_20_before_death_median"] = round(statistics.median(pre), 1)
    emean = [r["empty_mean"] for r in records if r.get("empty_mean") is not None]
    if emean:
        agg["empty_mean"] = round(statistics.mean(emean), 3)
    return agg


def _trajectory_lines(agg: dict[str, Any]) -> list[str]:
    """The 'why did they die' block: reservoir schedule + telemetry coverage."""
    sched = agg.get("reservoir_schedule")
    if not sched:
        return ["- reservoir schedule: - (run predates trajectory telemetry)"]
    parts = [
        f"k={k}: ply {v['birth_ply_median']:,.0f} "
        f"(min_e {v['birth_min_empty_mean']}, {v['reached_pct']:.0%} of games)"
        for k, v in sched.items()
    ]
    lines = ["- reservoir births: " + "  |  ".join(parts)]
    covered, games = agg.get("trajectory_games", 0), agg.get("games", 0)
    if covered < games:
        lines.append(
            f"- ⚠ trajectory telemetry covers only {covered}/{games} games of this pool "
            "(the rest predate it) — the blocks above and `hazard_100` describe THAT "
            "subset, not the pool."
        )
    return lines


def _write_summary_md(
    path: Path,
    cfg: dict[str, Any],
    run_id: str,
    agg: dict[str, Any],
    perf: dict[str, Any],
    seed_lo: int,
    seed_hi: int,
) -> None:
    p = agg["p_score_ge"]
    lines = [
        f"# Experiment `{cfg['label']}` — run `{run_id}`",
        "",
        f"- machine: **{perf['machine']}**  |  shard: **{perf['shard']}**  "
        f"|  seeds: **{seed_lo}..{seed_hi}** ({agg['games']} games)",
        f"- depth: **{cfg['depth']}**  |  workers: **{perf['workers']}**  "
        f"|  GOMAXPROCS: **{perf['gomaxprocs']}**",
        f"- notes: {cfg.get('notes', '')}",
        "",
        "## Tail metrics (THE headline — we need ONE game >= 48k, not a good mean)",
        "",
        f"- **max score: {agg['score_max']:,}**",
        f"- **P(score >= 48,000): {p['48000']:.1%}**  "
        f"(>=45k: {p['45000']:.1%}, >=50k: {p['50000']:.1%})",
        f"- reached 3x1024: **{agg['pct_reach_3x1024']:.1%}**  |  "
        f"reached 4x1024: **{agg['pct_reach_4x1024']:.1%}**  |  "
        f"reached 2x1024: {agg['pct_reach_2x1024']:.1%}",
        f"- forced-2048 rate: **{agg['forced_2048_rate']:.1%}**",
        "",
        "## MOVES — the metric the objective is really in (score = 18.2 x moves)",
        "",
        f"- **mu = {agg.get('moves_mean', 0):,}  sigma = {agg.get('moves_stdev', 0):,}**  "
        f"|  median {agg.get('moves_median', 0):,}  "
        f"|  IQR [{agg.get('moves_q25', 0):,}, {agg.get('moves_q75', 0):,}]  "
        f"|  max **{agg.get('moves_max', 0):,}**",
        f"- **fitted P(moves >= {agg.get('moves_target', 0):,}) = "
        f"{agg.get('p_moves_ge_target', 0):.4%}**  "
        f"(bootstrap 95% CI [{agg.get('p_moves_ge_target_ci95', [0, 0])[0]:.4%}, "
        f"{agg.get('p_moves_ge_target_ci95', [0, 0])[1]:.4%}])  "
        f"|  z = {agg.get('moves_z')}",
        "- rank arms by z / fitted P, NOT by the mean: the target is ~4.5 sigma out,",
        "  so sigma enters the objective exactly as hard as mu does.",
        "",
        "## Why the games died",
        "",
        "- end reasons: "
        + ("  ".join(f"{k}:{v}" for k, v in agg["end_reason_hist"].items()) or "-"),
        *_trajectory_lines(agg),
        f"- mean empty cells over the whole game: {agg.get('empty_mean', '-')}  |  "
        f"20 plies before death: {agg.get('empty_20_before_death_mean', '-')}",
        f"- taboo-blocked plies: {agg.get('taboo_blocked_plies_total', '-')} "
        f"({agg.get('taboo_blocked_frac', 0):.4%} of moves)"
        if "taboo_blocked_frac" in agg
        else "- taboo-blocked plies: -",
        "- hazard function P(death <= 100 plies | k reservoirs, e empties): see "
        "`hazard_100` in aggregates.json",
        "",
        "## Score distribution",
        "",
        f"- mean: {agg['score_mean']:,}  |  median: {agg['score_median']:,}  "
        f"|  min: {agg['score_min']:,}  |  stdev: {agg['score_stdev']:,}",
        "- max-tile histogram: "
        + ("  ".join(f"{k}:{v}" for k, v in agg["max_tile_hist"].items()) or "-"),
        "",
        "## Performance (compare machines: M4 vs Windows)",
        "",
        f"- wall time: **{perf['wall_s']:,.1f} s**  ({perf['wall_s'] / 3600:.2f} h)",
        f"- throughput: **{perf['games_per_hour']:,.0f} games/hour**  |  "
        f"**{perf['moves_per_sec']:,.0f} moves/s**",
        f"- engine cost: **{perf['ms_per_move_mean']} ms/move** (mean, single-thread) "
        if perf["ms_per_move_mean"] is not None
        else "- engine cost: n/a",
        "",
        "## Weights",
        "",
        "```json",
        json.dumps(cfg["weights"], indent=2),
        "```",
        "",
        "> Portable: this folder is self-contained. Zip it, move it, and merge",
        "> with other shards of the same profile via `scripts/experiment_merge.py`.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Experiment config JSON.")
    parser.add_argument(
        "--machine",
        default=None,
        help="Machine profile label (m4pro / m4 / windows / custom). Sets default "
        "workers + GOMAXPROCS and tags the run-id + perf.json.",
    )
    parser.add_argument("--workers", type=int, default=None, help="Override worker count.")
    parser.add_argument(
        "--shard",
        default="0/1",
        help="This machine's seed shard as IDX/COUNT (e.g. 0/2 = first half). "
        "Two machines with 0/2 and 1/2 run disjoint halves of the same seeds.",
    )
    parser.add_argument("--engine-bin", type=Path, default=None, help="Explicit Go engine binary.")
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Also write trace.jsonl: the FULL per-ply trajectory of every game "
        "([ply, dir, k, empty, legal, safe, score]). ~45 KB/game (~9 MB per 200-game "
        "run) vs ~2.5 KB/game for the windowed telemetry that is always on. Turn it "
        "on when you need to look at individual plies; the always-on summary already "
        "answers 'why did it die'.",
    )
    parser.add_argument(
        "--record-db",
        action="store_true",
        help="Also write full per-ply SQLite telemetry (data/experiments/<run>/wNN.db).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("research/experiments"),
        help="Parent dir for the run folder (default research/experiments).",
    )
    args = parser.parse_args()

    cfg = _load_config(args.config)

    # --- pin the engine and record WHAT IT ACTUALLY IS ------------------
    # Apply the config's env here, in the parent, so the `version` probe sees
    # exactly the environment the workers' engines will see: the reply then
    # reports the effective value of all 18 weights, including the ones this
    # config leaves to the engine's compiled-in defaults. Together with the
    # build hash, that is the run's provenance — without it a config.json is
    # only a partial description of the run that produced it.
    _apply_farm_env(cfg["weights"])
    from bot2048.ai.go_engine import engine_version

    engine_bin = args.engine_bin or cfg.get("engine_bin")
    engine_info = engine_version(Path(engine_bin) if engine_bin else None)

    # --- resolve machine profile + workers/gomaxprocs -------------------
    machine = args.machine or platform.node() or "unknown"
    profile = MACHINE_PROFILES.get(args.machine or "", {})
    workers = args.workers or profile.get("workers") or cfg.get("workers") or (os.cpu_count() or 4)
    gomaxprocs = int(cfg.get("gomaxprocs", profile.get("gomaxprocs", 1)))

    # --- resolve shard --------------------------------------------------
    try:
        shard_index, shard_count = (int(x) for x in args.shard.split("/"))
    except ValueError as exc:
        raise SystemExit(f"--shard must look like IDX/COUNT, got {args.shard!r}") from exc

    seeds_cfg = cfg["seeds"]
    base = int(seeds_cfg["base"])
    count = int(seeds_cfg["count"])
    shard_seeds = _shard_seeds(base, count, shard_index, shard_count)
    if not shard_seeds:
        raise SystemExit("empty shard — check --shard and seeds.count")
    seed_lo, seed_hi = shard_seeds[0], shard_seeds[-1]

    # --- build the per-run frozen config the workers see ----------------
    experiment_id = uuid.uuid4().hex
    stop_tile = int(cfg.get("stop_tile", 2048))
    stop_exponent = stop_tile.bit_length() - 1  # 2048 -> 11
    run_cfg: dict[str, Any] = {
        "weights": cfg["weights"],
        "depth": int(cfg.get("depth", 6)),
        "adaptive": bool(cfg.get("adaptive", False)),
        "gomaxprocs": gomaxprocs,
        "max_moves": cfg.get("max_moves", 6000),
        "stop_exponent": stop_exponent,
        "p4": cfg.get("p4"),
        # Pin the exact binary the version probe just identified, so every
        # worker runs the build we recorded (and not, say, a rebuild racing a
        # source edit mid-run).
        "engine_bin": engine_info["binary"],
        "experiment_id": experiment_id,
        "db_params": {
            "label": cfg["label"],
            "depth": int(cfg.get("depth", 6)),
            "farm": True,
            "weights": cfg["weights"],
            "shard": f"{shard_index}/{shard_count}",
            "machine": machine,
        },
    }

    # --- run folder -----------------------------------------------------
    run_id = _make_run_id(str(cfg["label"]), machine, shard_index, shard_count)
    run_dir = args.out / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    db_dir: Path | None = None
    if args.record_db:
        db_dir = Path("data/experiments") / run_id
        db_dir.mkdir(parents=True, exist_ok=True)

    chunks = _tasks(shard_seeds)
    pool_size = max(1, min(workers, len(chunks)))
    print(
        f">> run {run_id}\n"
        f"   seeds {seed_lo}..{seed_hi} ({len(shard_seeds)} games), "
        f"{pool_size} workers, depth {run_cfg['depth']}, GOMAXPROCS={gomaxprocs}\n"
        f"   engine {engine_info['build_sha256'][:12]} "
        f"(hoard_trigger={engine_info['BOT2048_FARM_HOARD_TRIGGER']}, "
        f"convex_w={engine_info['BOT2048_FARM_CONVEX_W']})"
    )

    payloads = [
        {
            "seeds": chunk,
            "cfg": run_cfg,
            # One db per GAME now that a task is one game. Named by seed, not by
            # worker index: with a pool queue there is no stable worker->seed
            # map to name a file after.
            "db_path": str(db_dir / f"g{chunk[0]:05d}.db") if db_dir else None,
            "trace": bool(args.trace),
        }
        for chunk in chunks
    ]

    started = time.perf_counter()
    records: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    engine_ms_sum = 0.0
    engine_ms_n = 0
    if len(payloads) == 1:
        outs = [_run_chunk(payloads[0])]
    else:
        # max_workers is the POOL SIZE, not the task count: the 200-odd
        # one-game tasks queue up behind `pool_size` processes and are handed
        # out as workers free up. That queue IS the load balancer.
        with ProcessPoolExecutor(max_workers=pool_size) as pool:
            futures = [pool.submit(_run_chunk, p) for p in payloads]
            outs = [fut.result() for fut in as_completed(futures)]
    for out in outs:
        records.extend(out["records"])
        traces.extend(out["traces"])
        engine_ms_sum += out["engine_ms_sum"]
        engine_ms_n += out["engine_ms_n"]
    wall_s = time.perf_counter() - started

    records.sort(key=lambda r: r["seed"])
    agg = _aggregate(records)

    total_moves = agg["moves_total"]
    perf = {
        "machine": machine,
        "shard": f"{shard_index}/{shard_count}",
        "workers": pool_size,
        "gomaxprocs": gomaxprocs,
        "depth": run_cfg["depth"],
        "games": len(records),
        "seed_base": base,
        "seed_count": count,
        "shard_seed_lo": seed_lo,
        "shard_seed_hi": seed_hi,
        "wall_s": round(wall_s, 3),
        "games_per_hour": round(len(records) / wall_s * 3600, 1) if wall_s > 0 else 0.0,
        "moves_per_sec": round(total_moves / wall_s, 1) if wall_s > 0 else 0.0,
        "moves_total": total_moves,
        "ms_per_move_mean": round(engine_ms_sum / engine_ms_n, 3) if engine_ms_n else None,
        "arch": platform.machine(),
        "python": platform.python_version(),
        "engine": engine_info,
    }

    # --- write the self-contained folder --------------------------------
    # config.json = the config VERBATIM + what the engine reported it actually
    # applied. The pair is the whole point: the config alone never described
    # the 5 weights it left implicit, nor which build ran them.
    cfg_out = dict(cfg)
    cfg_out["engine"] = engine_info
    cfg_out["p4_effective"] = _resolve_p4(cfg)
    (run_dir / "config.json").write_text(
        json.dumps(cfg_out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    (run_dir / "aggregates.json").write_text(json.dumps(agg, indent=2), encoding="utf-8")
    (run_dir / "perf.json").write_text(json.dumps(perf, indent=2), encoding="utf-8")
    if traces:
        traces.sort(key=lambda t: t["seed"])
        with (run_dir / "trace.jsonl").open("w", encoding="utf-8") as fh:
            for t in traces:
                fh.write(json.dumps(t, separators=(",", ":")) + "\n")
    _write_summary_md(run_dir / "summary.md", cfg, run_id, agg, perf, seed_lo, seed_hi)

    print(
        f"<< done in {wall_s:,.1f}s | moves mu {agg.get('moves_mean', 0):,.0f} "
        f"sigma {agg.get('moves_stdev', 0):,.0f} max {agg.get('moves_max', 0):,} "
        f"| P(moves>={TARGET_MOVES}) {agg.get('p_moves_ge_target', 0):.4%}\n"
        f"   score mean {agg['score_mean']:,.0f} max {agg['score_max']:,} "
        f"| 3x1024 {agg['pct_reach_3x1024']:.1%} 4x1024 {agg['pct_reach_4x1024']:.1%}\n"
        f"   -> {run_dir}"
    )


if __name__ == "__main__":
    main()

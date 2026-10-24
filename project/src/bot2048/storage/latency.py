"""Read-side move-latency report over ``moves.eval_details_json``.

SQL adapted from the validated prototype ``tmp/arch-proto/telemetry/report.py``:
the same nearest-rank percentile window pattern, but phase samples come from
``json_extract`` over the free-form ``eval_details_json`` (the ``timing``
sub-dict written by the live runner) instead of dedicated columns, and rows
group per experiment instead of per synthetic scenario.

Search timing is not duplicated in ``timing``: ``eval_ms`` is a real column
and ``$.engine_ms`` / ``$.prefetch_hit`` are written by the strategy wrapper
(P16). Here we derive::

    search_ipc  = eval_ms - engine_ms          (only when engine_ms is known)
    hidden      = prefetch_hit (P19: search ran behind the previous
                  animation and does not block the move); missing -> 0
    critical_ms = timing.total_ms + (1 - hidden) * eval_ms

Verdict ("слабина"): the phase with the largest MEAN contribution to the
critical path; search contributions are weighted by ``1 - hidden``.

Loop-gap metric (design-strategy-logging §5.1, the before/after yardstick for
the async recorder): the live runner stamps every committed move with
``$.move.t_commit`` (``time.monotonic()``) and the previous move's persist
cost ``$.move.prev_commit_ms``. Consecutive plies of one game give::

    gap_ms         = (t_commit[n] - t_commit[n-1]) * 1000
    unaccounted_ms = gap_ms - critical_ms[n]

i.e. everything between two commits that the phase timer never billed:
the recorder flush + cadence log of move n-1, the vision snapshot, the
ladder/control tick, loop overhead. Reported as p50/p95/p99/max per
experiment next to ``prev_commit_ms``; both stay silent for rows recorded
before the keys existed.

Read-only: the connection is opened with ``mode=ro`` -- no schema changes,
no writes, the frozen recording/storage contracts are untouched.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import structlog

from bot2048.storage.errors import StorageError

log = structlog.get_logger(__name__)

#: report phase name -> key inside ``eval_details_json  $.timing``
TIMING_PHASES: tuple[tuple[str, str], ...] = (
    ("capture", "capture_ms"),
    ("perceive", "perceive_ms"),
    ("detect", "detect_ms"),
    ("swipe", "swipe_ms"),
    ("settle", "settle_ms"),
)

# -- base CTE: one row per timed move, phases pulled out of the JSON blob --------
# ``prefetch_hit`` keeps NULL when absent (hit-rate is then unknown), while
# ``hidden`` coalesces to 0: an unknown prefetch is conservatively treated as
# blocking, so the critical path is never under-reported.
_TIMING_COLUMNS = ",\n           ".join(
    f"CAST(json_extract(m.eval_details_json, '$.timing.{key}') AS REAL) AS {key}"
    for _, key in (*TIMING_PHASES, ("total", "total_ms"))
)
_BASE_CTE = f"""
t AS (
    SELECT g.experiment_id AS experiment,
           m.game_id AS game_id,
           m.ply AS ply,
           {_TIMING_COLUMNS},
           m.eval_ms AS eval_ms,
           CAST(json_extract(m.eval_details_json, '$.engine_ms') AS REAL) AS engine_ms,
           json_extract(m.eval_details_json, '$.prefetch_hit') AS prefetch_hit,
           COALESCE(json_extract(m.eval_details_json, '$.prefetch_hit'), 0) AS hidden,
           CAST(json_extract(m.eval_details_json, '$.move.t_commit') AS REAL) AS t_commit,
           CAST(json_extract(m.eval_details_json, '$.move.prev_commit_ms') AS REAL)
               AS prev_commit_ms
    FROM moves m
    JOIN games g ON g.id = m.game_id
    WHERE json_extract(m.eval_details_json, '$.timing') IS NOT NULL
      AND (:exp IS NULL OR g.experiment_id = :exp)
)
"""

# -- SQL 1: nearest-rank percentiles per phase (validated window pattern) --------
_SAMPLES_UNION = "\n    UNION ALL\n".join(
    f"    SELECT experiment, '{name}' AS phase, {i} AS ord, {key} AS ms FROM t"
    for i, (name, key) in enumerate(TIMING_PHASES)
)
PERCENTILES_SQL = f"""
WITH {_BASE_CTE},
samples(experiment, phase, ord, ms) AS (
{_SAMPLES_UNION}
    UNION ALL
    SELECT experiment, 'search_ipc', 60, eval_ms - engine_ms
    FROM t WHERE engine_ms IS NOT NULL
    UNION ALL
    SELECT experiment, 'search_engine', 61, engine_ms
    FROM t WHERE engine_ms IS NOT NULL
    UNION ALL
    SELECT experiment, 'search_eval', 62, eval_ms FROM t
    UNION ALL
    SELECT experiment, 'TOTAL', 90, total_ms FROM t
    UNION ALL
    SELECT experiment, 'CRITICAL', 91, total_ms + (1 - hidden) * eval_ms FROM t
),
ranked AS (
    SELECT experiment, phase, ord, ms,
           ROW_NUMBER() OVER (PARTITION BY experiment, phase ORDER BY ms) AS rn,
           COUNT(*)    OVER (PARTITION BY experiment, phase)              AS n
    FROM samples
    WHERE ms IS NOT NULL
)
SELECT experiment, phase,
       MAX(CASE WHEN rn = ((n - 1) * 50) / 100 + 1 THEN ms END) AS p50,
       MAX(CASE WHEN rn = ((n - 1) * 90) / 100 + 1 THEN ms END) AS p90,
       MAX(CASE WHEN rn = ((n - 1) * 95) / 100 + 1 THEN ms END) AS p95,
       MAX(ms)                                                  AS max_ms
FROM ranked
GROUP BY experiment, phase, ord
ORDER BY experiment, ord
"""

# -- SQL 2: per-experiment critical-path summary + prefetch hit-rate -------------
# AVG skips NULLs, so ``hit_pct`` is NULL when no move ever recorded
# ``prefetch_hit`` -- surfaced to the caller as ``None``, never fabricated.
CRITICAL_SQL = f"""
WITH {_BASE_CTE}
SELECT experiment,
       COUNT(*)                                    AS moves,
       AVG(total_ms + eval_ms)                     AS avg_full,
       AVG(total_ms + (1 - hidden) * eval_ms)      AS avg_critical,
       AVG(prefetch_hit) * 100.0                   AS hit_pct,
       COUNT(engine_ms)                            AS engine_known
FROM t
GROUP BY experiment
ORDER BY experiment
"""

# -- SQL 2b: per-experiment reliability (predict-gate + FACTUAL capture kind) ----
# docs/research/telemetry_review.md M2/M3/N6. The predict-gate outcome and the
# FACTUAL frame source now ride in ``eval_details.move`` (written per move by
# the live runner); the report reads the FACT instead of the configured
# ``experiments.params_json.capture`` so a silent scrcpy->screencap fallback is
# visible. All counters are NULL-safe: an experiment whose moves predate this
# telemetry simply reports 0 gate-moves / 0 kind-known and the caller keeps the
# configured capture label.
RELIABILITY_SQL = """
SELECT g.experiment_id AS experiment,
       SUM(CASE WHEN CAST(json_extract(m.eval_details_json, '$.move.gate_frames')
                     AS INTEGER) > 0 THEN 1 ELSE 0 END)                    AS gate_moves,
       SUM(CASE WHEN json_extract(m.eval_details_json, '$.move.gate_hit') = 1
                THEN 1 ELSE 0 END)                                         AS gate_hits,
       SUM(CASE WHEN json_extract(m.eval_details_json, '$.move.gate_fallback') = 1
                THEN 1 ELSE 0 END)                                         AS gate_fallbacks,
       SUM(CASE WHEN json_extract(m.eval_details_json, '$.move.capture_kind')
                     IS NOT NULL THEN 1 ELSE 0 END)                        AS kind_known,
       SUM(CASE WHEN json_extract(m.eval_details_json, '$.move.capture_kind')
                     = 'screencap' THEN 1 ELSE 0 END)                      AS screencap_moves,
       SUM(CASE WHEN json_extract(m.eval_details_json, '$.move.apk_age_ms')
                     IS NOT NULL THEN 1 ELSE 0 END)                        AS apk_moves,
       AVG(CAST(json_extract(m.eval_details_json, '$.move.apk_age_ms')
                AS REAL))                                                  AS apk_age_avg
FROM moves m
JOIN games g ON g.id = m.game_id
WHERE json_extract(m.eval_details_json, '$.timing') IS NOT NULL
  AND (:exp IS NULL OR g.experiment_id = :exp)
GROUP BY g.experiment_id
"""

# -- SQL 2c: loop gap -- unaccounted_ms + prev_commit_ms percentiles ---------------
# ``LAG`` pairs consecutive stamped plies of ONE game (WHERE runs before the
# window, so rows without ``t_commit`` -- older telemetry -- never break a
# pair; a mixed DB at worst spans one gap over the unstamped rows). The
# critical path subtracted is that of move n, the move whose timed phases lie
# INSIDE the gap ``n-1 -> n``; ``prev_commit_ms`` on the same row is the
# persist cost of move n-1, also inside that gap. p99 joins the usual
# nearest-rank set because the metric exists to catch the flush/checkpoint
# spikes, which live in the tail.
GAP_SQL = f"""
WITH {_BASE_CTE},
gaps AS (
    SELECT experiment,
           prev_commit_ms,
           (t_commit - LAG(t_commit) OVER (PARTITION BY game_id ORDER BY ply)) * 1000.0
               - (total_ms + (1 - hidden) * eval_ms) AS unaccounted_ms
    FROM t
    WHERE t_commit IS NOT NULL
),
samples(experiment, metric, ms) AS (
    SELECT experiment, 'unaccounted', unaccounted_ms FROM gaps
    WHERE unaccounted_ms IS NOT NULL
    UNION ALL
    SELECT experiment, 'prev_commit', prev_commit_ms FROM gaps
    WHERE prev_commit_ms IS NOT NULL
),
ranked AS (
    SELECT experiment, metric, ms,
           ROW_NUMBER() OVER (PARTITION BY experiment, metric ORDER BY ms) AS rn,
           COUNT(*)    OVER (PARTITION BY experiment, metric)              AS n
    FROM samples
)
SELECT experiment, metric,
       MAX(n)                                                   AS n,
       MAX(CASE WHEN rn = ((n - 1) * 50) / 100 + 1 THEN ms END) AS p50,
       MAX(CASE WHEN rn = ((n - 1) * 95) / 100 + 1 THEN ms END) AS p95,
       MAX(CASE WHEN rn = ((n - 1) * 99) / 100 + 1 THEN ms END) AS p99,
       MAX(ms)                                                  AS max_ms
FROM ranked
GROUP BY experiment, metric
ORDER BY experiment, metric
"""

# -- SQL 3: mean contribution of every phase to the critical path ----------------
# Both the engine/IPC split (NULL-skipped when engine_ms is absent) and the
# combined ``search`` fallback are computed; the caller keeps exactly one.
_CONTRIB_UNION = "\n    UNION ALL\n".join(
    f"    SELECT experiment, '{name}' AS phase, AVG({key}) AS avg_ms"
    f" FROM t GROUP BY experiment"
    for name, key in TIMING_PHASES
)
BOTTLENECK_SQL = f"""
WITH {_BASE_CTE}
{_CONTRIB_UNION}
    UNION ALL
    SELECT experiment, 'search_engine', AVG(engine_ms * (1 - hidden))
    FROM t WHERE engine_ms IS NOT NULL GROUP BY experiment
    UNION ALL
    SELECT experiment, 'search_ipc', AVG((eval_ms - engine_ms) * (1 - hidden))
    FROM t WHERE engine_ms IS NOT NULL GROUP BY experiment
    UNION ALL
    SELECT experiment, 'search', AVG(eval_ms * (1 - hidden))
    FROM t GROUP BY experiment
"""


@dataclass(frozen=True)
class PhaseRow:
    """One percentile row of the latency table."""

    phase: str
    p50: float
    p90: float
    p95: float
    max_ms: float


@dataclass(frozen=True)
class Contribution:
    """Mean contribution of one phase to the critical path."""

    phase: str
    avg_ms: float
    share_pct: float


@dataclass(frozen=True)
class GapRow:
    """Percentiles of one loop-gap metric (``unaccounted`` / ``prev_commit``)."""

    metric: str
    n: int  # samples: stamped ply pairs (unaccounted) / rows with the key (prev_commit)
    p50: float
    p95: float
    p99: float
    max_ms: float


@dataclass(frozen=True)
class ExperimentLatency:
    """The full latency report of one experiment."""

    experiment_id: str
    capture_mode: str | None  # CONFIGURED capture (experiments.params_json)
    moves: int
    avg_full_ms: float
    avg_critical_ms: float
    prefetch_hit_pct: float | None
    percentiles: tuple[PhaseRow, ...]
    contributions: tuple[Contribution, ...]  # ranked, largest first
    # -- reliability (docs/research/telemetry_review.md M2/M3/N6) -------------------
    gate_hit_pct: float | None = None  # predict-gate acceptances / gate-moves
    gate_fallback_pct: float | None = None  # predict-gate timeouts / gate-moves
    capture_kind_known: int = 0  # moves that reported a FACTUAL capture_kind
    capture_screencap_pct: float | None = None  # of those, served by screencap
    apk_moves: int = 0  # moves verified from an APK packet that reported its age
    apk_age_avg_ms: float | None = None  # mean host mailbox dwell of those packets
    # -- loop gap (design-strategy-logging §5.1) ------------------------------------
    gaps: tuple[GapRow, ...] = ()  # empty when no move carried ``t_commit``

    @property
    def bottleneck(self) -> str:
        """The verdict: the phase dominating the critical path."""
        return self.contributions[0].phase

    @property
    def unaccounted(self) -> GapRow | None:
        """``gap_ms - critical_ms`` percentiles, ``None`` before the stamp existed."""
        return self._gap("unaccounted")

    @property
    def prev_commit(self) -> GapRow | None:
        """Persist-section cost percentiles, ``None`` before the key existed."""
        return self._gap("prev_commit")

    def _gap(self, metric: str) -> GapRow | None:
        return next((row for row in self.gaps if row.metric == metric), None)


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open the telemetry database strictly read-only (``mode=ro`` URI)."""
    path = Path(db_path)
    if not path.is_file():
        raise StorageError(f"telemetry database not found: {path}")
    try:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - environment-specific
        raise StorageError(f"failed to open telemetry db {path}: {exc}") from exc


def collect_latency(
    conn: sqlite3.Connection, experiment: str | None = None
) -> tuple[ExperimentLatency, ...]:
    """Aggregate per-phase latency for every experiment with timed moves."""
    args = {"exp": experiment}
    try:
        pct = conn.execute(PERCENTILES_SQL, args).fetchall()
        crit = conn.execute(CRITICAL_SQL, args).fetchall()
        contrib = conn.execute(BOTTLENECK_SQL, args).fetchall()
        reliability = {r[0]: r for r in conn.execute(RELIABILITY_SQL, args).fetchall()}
        gap_rows = conn.execute(GAP_SQL, args).fetchall()
        capture_modes = _capture_modes(conn)
    except sqlite3.Error as exc:
        raise StorageError(f"latency query failed: {exc}") from exc

    reports: list[ExperimentLatency] = []
    for experiment_id, moves, avg_full, avg_critical, hit_pct, engine_known in crit:
        rows = tuple(
            PhaseRow(phase=r[1], p50=r[2], p90=r[3], p95=r[4], max_ms=r[5])
            for r in pct
            if r[0] == experiment_id
        )
        ranked = _rank_contributions(
            [(r[1], r[2]) for r in contrib if r[0] == experiment_id],
            engine_split=engine_known > 0,
        )
        (
            gate_hit_pct,
            gate_fallback_pct,
            kind_known,
            screencap_pct,
            apk_moves,
            apk_age_avg_ms,
        ) = _reliability(reliability.get(experiment_id))
        gaps = tuple(
            GapRow(metric=r[1], n=int(r[2]), p50=r[3], p95=r[4], p99=r[5], max_ms=r[6])
            for r in gap_rows
            if r[0] == experiment_id
        )
        reports.append(
            ExperimentLatency(
                experiment_id=experiment_id,
                capture_mode=capture_modes.get(experiment_id),
                moves=moves,
                avg_full_ms=avg_full,
                avg_critical_ms=avg_critical,
                prefetch_hit_pct=hit_pct,
                percentiles=rows,
                contributions=ranked,
                gate_hit_pct=gate_hit_pct,
                gate_fallback_pct=gate_fallback_pct,
                capture_kind_known=kind_known,
                capture_screencap_pct=screencap_pct,
                apk_moves=apk_moves,
                apk_age_avg_ms=apk_age_avg_ms,
                gaps=gaps,
            )
        )
    log.debug(
        "latency.collected",
        experiments=len(reports),
        filter=experiment,
    )
    return tuple(reports)


def _rank_contributions(
    raw: list[tuple[str, float | None]], *, engine_split: bool
) -> tuple[Contribution, ...]:
    """Drop the redundant search rows, rank by mean cost, compute shares.

    When ``engine_ms`` is known for at least one move the engine/IPC split is
    kept and the combined ``search`` fallback dropped; otherwise the split
    rows are all-NULL and the combined phase represents the whole decide.
    """
    drop = {"search"} if engine_split else {"search_engine", "search_ipc"}
    kept = [(name, avg) for name, avg in raw if name not in drop and avg is not None]
    kept.sort(key=lambda item: item[1], reverse=True)
    crit_total = sum(avg for _, avg in kept)
    return tuple(
        Contribution(
            phase=name,
            avg_ms=avg,
            share_pct=(avg * 100.0 / crit_total) if crit_total > 0 else 0.0,
        )
        for name, avg in kept
    )


def _reliability(
    row: tuple[str, int, int, int, int, int, int, float | None] | None,
) -> tuple[float | None, float | None, int, float | None, int, float | None]:
    """``(gate_hit_pct, gate_fallback_pct, kind_known, screencap_pct, apk_moves,
    apk_age_avg_ms)`` from a reliability row.

    Percentages stay ``None`` when their denominator is zero (no gate-moves /
    no move ever reported a FACTUAL capture kind) so the caller never fabricates
    a rate for an experiment recorded before this telemetry existed; likewise
    ``apk_age_avg_ms`` is ``None`` when no move carried ``apk_age_ms`` (frame
    path only, or a source that cannot measure the mailbox dwell).
    """
    if row is None:
        return (None, None, 0, None, 0, None)
    _, gate_moves, gate_hits, gate_fallbacks, kind_known, screencap, apk_moves, apk_age = row
    gate_hit_pct = 100.0 * gate_hits / gate_moves if gate_moves else None
    gate_fallback_pct = 100.0 * gate_fallbacks / gate_moves if gate_moves else None
    screencap_pct = 100.0 * screencap / kind_known if kind_known else None
    return (
        gate_hit_pct,
        gate_fallback_pct,
        int(kind_known),
        screencap_pct,
        int(apk_moves or 0),
        float(apk_age) if apk_age is not None else None,
    )


def _capture_modes(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Capture mode per experiment, read from ``experiments.params_json``."""
    modes: dict[str, str | None] = {}
    for experiment_id, params_json in conn.execute(
        "SELECT id, params_json FROM experiments"
    ):
        try:
            params = json.loads(params_json)
        except (TypeError, ValueError):  # malformed row: report, don't crash
            log.warning("latency.bad_params_json", experiment_id=experiment_id)
            params = {}
        capture = params.get("capture") if isinstance(params, dict) else None
        modes[experiment_id] = str(capture) if capture is not None else None
    return modes

"""Per-game trajectory telemetry: WHY a game died, not just that it did.

``results.jsonl`` used to carry the game's *epitaph* only (score, moves, max
tile, end reason). When profile A lost to profile B, nothing in the run folder
said why — and every interesting finding of the 2026-07-11 session had to be
dug out with a throwaway instrumentation script, rewritten from scratch by each
agent. This module is that instrumentation, made permanent and free.

It is a :class:`~bot2048.recording.Recorder`: it snoops the events
``sim.runner.play_game`` already emits, so it needs no engine change and no
second pass over the game. Everything is derived from the DECISION states — the
board the bot actually faced at ply *t* (``MovePlayed.board_before``) — which is
the state a risk model must condition on.

What it derives, and why each thing is here (docs/archive/audit-2026-07-11.md §3.3,
docs/tail-analysis.md):

* **reservoir birth schedule** — the ply the k-th 1024 first appeared. Measured
  491 / 957 / 1420 / 1895: a step of 466 = 1024/2.2, i.e. reservoirs arrive on
  the *mass* schedule and nothing is wasted. A profile that shifts this schedule
  has changed the game's whole shape.
* **empty-cell history** (windowed, plus a window around each birth) — the bot
  dies of CONGESTION, so ``e`` is the state variable. The 4th reservoir is born
  at ``min e = 0.00``: that is the crisis, and it is invisible in a summary row.
* **(k, e) occupancy + deaths within 100 plies** — pooled over a run, these two
  histograms ARE the hazard function ``P(death <= 100 | k, e)``. It used to take
  a bespoke script; now every run pays for it in full.
* **legal / taboo-safe direction counts** — a 2048 merge ends the game, so the
  choice set is the legal directions that do NOT assemble one. (Measured: the
  taboo binds on 0.027% of plies. Cheap to keep watching, since the term becomes
  load-bearing exactly when the bot starts building the 4th reservoir.)
* **the death itself** — reason, and the last plies leading into it, so a
  post-mortem does not need the game replayed.

Cost (measured, not estimated): the only non-trivial per-ply work is the
legal/safe scan — one bitboard pack and four table moves, **4.0 us/ply**;
everything else is ``tuple.count`` and list appends (0.4 us/ply). Against an
engine that spends ~23 ms/ply thinking, that is **~0.02%**. Nothing was added to
the Go engine, which is where the time actually goes.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from bot2048.core import engine
from bot2048.core.types import Board, Direction

__all__ = [
    "BIRTH_WINDOW",
    "EMPTY_WINDOW",
    "HAZARD_HORIZON",
    "RANK_1024",
    "TelemetryRecorder",
    "TrajectoryTracker",
    "hazard_table",
]

RANK_1024 = 10  # exponent of a 1024 tile ("reservoir")
_HEX_DIGITS = "0123456789abcdef"

#: Plies per bucket of the empty-cell history written to ``results.jsonl``.
EMPTY_WINDOW = 100
#: Window around a reservoir birth summarised as that birth's "crisis".
#: Asymmetric on purpose: the squeeze happens while the bot is *assembling* the
#: reservoir (the run-up), and relaxes once the tile lands.
BIRTH_WINDOW = (-60, 20)
#: Horizon of the hazard function: P(death within this many plies | k, e).
HAZARD_HORIZON = 100
#: Plies of context kept around the death.
DEATH_TAIL = 20

_DEATHS: frozenset[str] = frozenset({"game_over", "tile_cap"})

_DIRECTIONS: tuple[Direction, ...] = tuple(Direction)
# Nibble bit-plane mask. A cell holds rank 11 (2048) iff its bits 0, 1 and 3 are
# all set (11 = 0b1011). Rank 12+ can never appear — the taboo stops the game at
# 11 — so ``bb & (bb >> 1) & (bb >> 3) & _B0`` is an exact 2048 test.
_B0 = 0x1111111111111111


def _choice_set(board: Board) -> tuple[int, int]:
    """``(legal, safe)`` direction counts for a decision state.

    ``safe`` = legal directions that do not assemble 2048 — the choice set the
    bot is really picking from, since a 2048 merge ends the game.

    Goes through the engine's BITBOARD helpers rather than the public
    ``engine.move``: one pack and four table moves, with no tuple unpacked. This
    runs once per ply of every simulated game, and the difference is measured —
    4.0 us/ply vs 11.1 us/ply for four ``engine.move`` calls. (Against a ~23
    ms/ply engine either is noise, but the cheap one is also the simpler one:
    it needs no "skip this when k < 2" special case to stay cheap.)
    """
    bb = engine._pack(board)
    legal = 0
    safe = 0
    for d in _DIRECTIONS:
        nbb, _ = engine._move_bb(bb, d)
        if nbb == bb:
            continue
        legal += 1
        if not (nbb & (nbb >> 1) & (nbb >> 3) & _B0):
            safe += 1
    return legal, safe


@dataclass(slots=True)
class TrajectoryTracker:
    """Accumulates one game's trajectory, then reduces it to a summary dict."""

    initial_board: Board
    trace: bool = False

    # Per-ply decision-state history (one entry per played move).
    empties: list[int] = field(default_factory=list)
    reservoirs: list[int] = field(default_factory=list)
    legals: list[int] = field(default_factory=list)
    safes: list[int] = field(default_factory=list)
    dirs: list[int] = field(default_factory=list)
    scores: list[int] = field(default_factory=list)

    births: dict[int, int] = field(default_factory=dict)
    peak_1024: int = 0
    final_board: Board = ()
    engine_ms_sum: float = 0.0
    engine_ms_n: int = 0

    # Filled by GameEnded.
    end_reason: str = ""
    num_moves: int = 0

    def __post_init__(self) -> None:
        self.final_board = self.initial_board

    # -- event ingestion (hot path: keep it to appends) --------------------
    def on_move(self, board_before: Board, direction: int, score_total: int) -> None:
        legal, safe = _choice_set(board_before)
        self.empties.append(board_before.count(0))
        self.reservoirs.append(board_before.count(RANK_1024))
        self.legals.append(legal)
        self.safes.append(safe)
        if self.trace:
            self.dirs.append(int(direction))
            self.scores.append(score_total)

    def on_board(self, board: Board) -> None:
        """Any board the game passed through (post-move or post-spawn)."""
        self.final_board = board
        c = board.count(RANK_1024)
        if c > self.peak_1024:
            # A 1024 can only be CREATED by a move (spawns are 2s and 4s), and
            # on_move for that move has already appended its decision state — so
            # the ply that made it is len - 1.
            ply = max(0, len(self.empties) - 1)
            for k in range(self.peak_1024 + 1, c + 1):
                self.births.setdefault(k, ply)
            self.peak_1024 = c

    def on_engine_ms(self, ms: float) -> None:
        self.engine_ms_sum += ms
        self.engine_ms_n += 1

    # -- reduction ---------------------------------------------------------
    def _empty_windows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for lo in range(0, len(self.empties), EMPTY_WINDOW):
            chunk = self.empties[lo : lo + EMPTY_WINDOW]
            out.append(
                {
                    "from": lo,
                    "to": lo + len(chunk),
                    "mean": round(statistics.mean(chunk), 3),
                    "median": statistics.median(chunk),
                    "min": min(chunk),
                }
            )
        return out

    def _birth_crisis(self) -> dict[str, dict[str, float]]:
        """Empty-cell squeeze around each reservoir's birth.

        The headline number of the whole failure mode lives here: the 4th
        reservoir is born with ``min_empty = 0`` — the bot spends its entire
        structure on it and has nothing left.
        """
        lo_off, hi_off = BIRTH_WINDOW
        out: dict[str, dict[str, float]] = {}
        n = len(self.empties)
        for k, ply in sorted(self.births.items()):
            lo = max(0, ply + lo_off)
            hi = min(n, ply + hi_off)
            chunk = self.empties[lo:hi]
            if not chunk:
                continue
            out[str(k)] = {
                "min_empty": min(chunk),
                "mean_empty": round(statistics.mean(chunk), 3),
            }
        return out

    def _risk(self) -> tuple[dict[str, int], dict[str, int]]:
        """``(visits, deaths)`` keyed ``"k,e"`` — the hazard function's raw counts.

        A ply is *observed* only if its fate within :data:`HAZARD_HORIZON` is
        known: either the game died (every ply's fate is then known) or the ply
        is far enough from a censoring abort. Counting a censored ply as a
        survivor would bias the hazard DOWN exactly where it matters.
        """
        visits: dict[str, int] = {}
        deaths: dict[str, int] = {}
        died = self.end_reason in _DEATHS
        n = len(self.empties)
        for t in range(n):
            if not died and (n - t) <= HAZARD_HORIZON:
                continue  # censored: the game was cut off, not killed
            key = f"{self.reservoirs[t]},{self.empties[t]}"
            visits[key] = visits.get(key, 0) + 1
            if died and (n - t) <= HAZARD_HORIZON:
                deaths[key] = deaths.get(key, 0) + 1
        return visits, deaths

    def _death(self) -> dict[str, Any]:
        n = len(self.empties)
        tail: list[list[int]] = []
        for t in range(max(0, n - DEATH_TAIL), n):
            tail.append([t, self.reservoirs[t], self.empties[t], self.legals[t], self.safes[t]])
        idx = n - DEATH_TAIL
        return {
            "reason": self.end_reason,
            "moves": self.num_moves,
            # The state DEATH_TAIL plies before the end: what the board looked
            # like while the game was still winnable-looking.
            "empty_at_minus20": self.empties[idx] if idx >= 0 else None,
            "k_at_minus20": self.reservoirs[idx] if idx >= 0 else None,
            "safe_at_minus20": self.safes[idx] if idx >= 0 else None,
            # [ply, k, empty, legal, safe] per ply, oldest first.
            "tail": tail,
        }

    def summary(self) -> dict[str, Any]:
        visits, deaths = self._risk()
        legal_hist: dict[str, int] = {}
        safe_hist: dict[str, int] = {}
        for v in self.legals:
            legal_hist[str(v)] = legal_hist.get(str(v), 0) + 1
        for v in self.safes:
            safe_hist[str(v)] = safe_hist.get(str(v), 0) + 1
        blocked = sum(1 for i in range(len(self.legals)) if self.safes[i] < self.legals[i])
        return {
            "peak_1024": self.peak_1024,
            "final_1024": self.final_board.count(RANK_1024) if self.final_board else 0,
            "final_board": "".join(_HEX_DIGITS[e] for e in self.final_board)
            if self.final_board
            else None,
            "engine_ms_mean": round(self.engine_ms_sum / self.engine_ms_n, 3)
            if self.engine_ms_n
            else None,
            "reservoir_births": {str(k): v for k, v in sorted(self.births.items())},
            "birth_crisis": self._birth_crisis(),
            "empty_windows": self._empty_windows(),
            "empty_mean": round(statistics.mean(self.empties), 3) if self.empties else None,
            "empty_min": min(self.empties) if self.empties else None,
            "risk_visits": visits,
            "risk_deaths": deaths,
            "legal_dirs_hist": legal_hist,
            "safe_dirs_hist": safe_hist,
            "taboo_blocked_plies": blocked,
            "death": self._death(),
        }

    def trace_rows(self) -> list[list[int]]:
        """Full per-ply trajectory: ``[ply, dir, k, empty, legal, safe, score]``."""
        if not self.trace:
            return []
        return [
            [t, self.dirs[t], self.reservoirs[t], self.empties[t], self.legals[t], self.safes[t],
             self.scores[t]]
            for t in range(len(self.dirs))
        ]


class TelemetryRecorder:
    """Recorder that builds a :class:`TrajectoryTracker` per game.

    Optionally tees every event to an inner recorder (e.g. ``SqliteRecorder``)
    so full per-ply replay still works alongside.
    """

    def __init__(self, inner: Any | None = None, *, trace: bool = False) -> None:
        self._inner = inner
        self._trace = trace
        self.games: dict[str, TrajectoryTracker] = {}

    def record(self, event: Any) -> None:
        if self._inner is not None:
            self._inner.record(event)
        name = type(event).__name__
        if name == "GameStarted":
            self.games[event.game_id] = TrajectoryTracker(
                initial_board=event.initial_board, trace=self._trace
            )
            return
        g = self.games.get(event.game_id)
        if g is None:
            return
        if name == "MovePlayed":
            g.on_move(event.board_before, int(event.direction), event.score_total)
            g.on_board(event.board_after)
            ms = event.eval_details.get("engine_ms")
            if isinstance(ms, (int, float)):
                g.on_engine_ms(float(ms))
        elif name == "TileSpawned":
            g.on_board(event.board_after)
        elif name == "GameEnded":
            g.end_reason = event.end_reason
            g.num_moves = event.num_moves

    def pop(self, game_id: str) -> TrajectoryTracker | None:
        """Take a finished game's tracker out (frees its per-ply history)."""
        return self.games.pop(game_id, None)

    def flush(self) -> None:
        if self._inner is not None:
            self._inner.flush()

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()


def hazard_table(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pool per-game ``risk_visits``/``risk_deaths`` into the hazard function.

    Returns ``{"k,e": {"visits": n, "deaths": d, "p_death_100": d/n}}`` — the
    same table that docs/tail-analysis.md needed a one-off script to build.
    Games written before this telemetry existed simply carry neither key and
    contribute nothing (the table is then empty, not wrong).
    """
    visits: dict[str, int] = {}
    deaths: dict[str, int] = {}
    for r in records:
        for key, n in (r.get("risk_visits") or {}).items():
            visits[key] = visits.get(key, 0) + int(n)
        for key, n in (r.get("risk_deaths") or {}).items():
            deaths[key] = deaths.get(key, 0) + int(n)
    out: dict[str, dict[str, Any]] = {}
    for key, n in sorted(visits.items(), key=lambda kv: tuple(int(x) for x in kv[0].split(","))):
        d = deaths.get(key, 0)
        out[key] = {"visits": n, "deaths": d, "p_death_100": round(d / n, 4)}
    return out

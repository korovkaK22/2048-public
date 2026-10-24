"""Declarative strategy description -- ``StrategySpec`` and friends.

A "strategy" used to be assembled from six places with no single record of
what actually played (Python ctor args, the engine binary, ~35 engine env
vars, the weight file probed off the disk, per-request protocol fields and
the live ``BudgetLadder``). This module is the ONE description of all of it:
a frozen, hashable, JSON-serialisable tree whose ``spec_hash`` pins the
strategy and whose ``label`` (``id@hash8``) is what goes into
``Strategy.name`` / ``experiments.strategy``.

Nothing here talks to an engine. The factory/resolver that builds a
``Strategy`` from a spec and checks the engine's ``version`` reply against
``engine.env`` lives in a later step (``ai/factory.py``); the JSON registry
that stores specs by name is :mod:`bot2048.ai.registry`.

Design rules this module enforces:

* **Env completeness.** For the Go engines ``engine.env`` must carry EVERY key
  the engine reads from its environment (the set each engine reports in its
  ``version`` reply, see :data:`ENGINE_ENV_KEYS`), so an "implicit compiled-in
  default" can never hide behind a spec again. Extra ``BOT2048_*`` keys are
  allowed (a Go knob may land before this list catches up); anything else is
  rejected.
* **One truth per knob.** ``search.cprob`` / ``search.workers`` duplicate the
  env keys ``BOT2048_CPROB_THRESH`` / ``BOT2048_SEARCH_WORKERS`` on purpose
  (the first is what the client reasons about, the second is what the engine
  reads); the two must agree or the spec is rejected.
* **Two independent search limits** (:class:`Limits`): ``max_depth`` and
  ``budget_ms``, each optional, the engine stops at whichever hits first;
  ``None`` = no limit on that axis. :class:`Guard` is always present: the node
  cap is the deterministic safety net when both limits are ``None``.
* **Ladder monotonicity** (:class:`LadderSpec`): within a game the tiers
  T0 -> T1 -> T2 may raise either axis or lift it to ``None`` but never shrink
  it (``None`` counts as the largest value).
* ``notes`` is the only field excluded from the hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, cast, get_args

__all__ = [
    "ENGINE_ENV_KEYS",
    "ENGINE_KINDS",
    "GO_ENGINE_KINDS",
    "SPEC_SCHEMA",
    "TRIVIAL_STRATEGY_IDS",
    "EngineKind",
    "EngineSpec",
    "Guard",
    "LadderSpec",
    "Limits",
    "SearchSpec",
    "SpecError",
    "StrategySpec",
    "limits_shrink_axis",
]

#: Bumped whenever the JSON shape changes incompatibly. A spec written under a
#: different schema is refused rather than silently reinterpreted.
SPEC_SCHEMA = 1

EngineKind = Literal["engine2048", "ntuple", "python", "trivial"]

#: Every ``EngineSpec.kind`` value, as a set for validation.
ENGINE_KINDS: frozenset[str] = frozenset(get_args(EngineKind))

#: The kinds that run as a Go subprocess with an environment to pin.
GO_ENGINE_KINDS: frozenset[str] = frozenset({"engine2048", "ntuple"})

#: ``kind == "trivial"`` dispatches on the spec's base id (random/greedy/corner).
TRIVIAL_STRATEGY_IDS: frozenset[str] = frozenset({"random", "greedy", "corner"})

#: Width of the content hash (hex chars) and of the short hash in ``label``.
HASH_HEX = 16
HASH8_HEX = 8

# -- engine environment --------------------------------------------------------
#
# Derived from the engines' `version` replies (what each engine ACTUALLY reads
# from its environment at startup and reports back):
#   tools/engine2048/main.go  handleVersion  -- 18 farm weights + cprob + pool
#   tools/ntuple/serve.go     handleVersion  -- 13 farm weights + cprob + pool
#                                              + leaf switches + record levers
# Keys the reply derives from something else (NTUPLE_WEIGHTS/OBJECTIVE/
# LAMBDA/BASE come from the weight file, NTUPLE_PREFETCH_WORKERS from
# GOMAXPROCS) are not env inputs and are deliberately absent here.

_FARM_COMMON: tuple[str, ...] = (
    "EMPTY_W",
    "MERGE_W",
    "CORNER_W",
    "EDGE_W",
    "STAIR_W",
    "SNAKE_W",
    "SNAKE_RATIO",
    "CORNER_RANK_W",
    "CONVEX_W",
    "LATE_EMPTY_W",
    "LATE_GRAD_MULT",
    "LATE_CORNER_RANK_W",
    "HOARD_TRIGGER",
)
# The five newer farm terms exist only in engine2048 (the two "farm" leaves
# have diverged; they are NOT the same evaluator).
_FARM_ENGINE2048_ONLY: tuple[str, ...] = (
    "MOBILITY_W",
    "MOB_PAIR_PEN",
    "QUADCORNER_W",
    "QUAD_RATIO",
    "EMPTY_CURVE_W",
)
_RECORD_LEVERS: tuple[str, ...] = (
    "RECORD_ENGINE",
    "TARGET",
    "EXACT_EMPTIES",
    "EXACT_NODES",
    "TARGET_PCONF",
    "EXACT_EARLYFIRE",
    "EXACT_EARLY_EMPTIES",
    "MAXCASH_DEPTH",
    "SALVAGE_KAPPA",
)
_SEARCH_COMMON: tuple[str, ...] = (
    "GOMAXPROCS",
    "BOT2048_SEARCH_WORKERS",
    "BOT2048_CPROB_THRESH",
)

ENGINE2048_ENV_KEYS: frozenset[str] = frozenset(
    (*_SEARCH_COMMON, *(f"BOT2048_FARM_{k}" for k in (*_FARM_COMMON, *_FARM_ENGINE2048_ONLY)))
)
NTUPLE_ENV_KEYS: frozenset[str] = frozenset(
    (
        *_SEARCH_COMMON,
        *(f"BOT2048_FARM_{k}" for k in _FARM_COMMON),
        "BOT2048_NTUPLE_MOB_W",
        "BOT2048_EVAL_MLP",
        "BOT2048_LEAF_CACHE",
        *(f"BOT2048_{k}" for k in _RECORD_LEVERS),
    )
)

#: Per engine kind: the env keys a complete spec MUST pin. Python/trivial
#: strategies have no process environment, so theirs is empty (and any env on
#: them is an error).
ENGINE_ENV_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "engine2048": ENGINE2048_ENV_KEYS,
        "ntuple": NTUPLE_ENV_KEYS,
        "python": frozenset(),
        "trivial": frozenset(),
    }
)

#: Env keys that are NOT engine configuration and must not appear in a spec:
#: the weight file is ``EngineSpec.weights`` (resolved to a path by the
#: factory), the binary is chosen by the factory from ``kind``.
_FORBIDDEN_ENV_KEYS: frozenset[str] = frozenset(
    {"BOT2048_NTUPLE_WEIGHTS", "BOT2048_ENGINE2048_BIN"}
)
_ENV_KEY_RE = re.compile(r"^(BOT2048_[A-Z0-9_]+|GOMAXPROCS)$")

#: ``id`` grammar: a registry name (lowercase, digits, ``.``/``_``/``-``) with
#: an optional ``+hash8`` suffix marking a derived (overridden) spec. ``@`` is
#: reserved for ``label``.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(\+[0-9a-f]{8})?$")

#: Largest tile exponent a 4-bit nibble board can hold (2**16 = 65536).
_MAX_EXPONENT = 16


class SpecError(ValueError):
    """A spec is malformed: bad type, unknown key, inconsistent fields."""


# -- strict (de)serialisation helpers -----------------------------------------


def _check_keys(data: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise SpecError(f"{where}: unknown key(s) {unknown}")


def _as_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecError(f"{where}: expected an object, got {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise SpecError(f"{where}: non-string key {key!r}")
    return value


def _opt_int(data: Mapping[str, Any], key: str, where: str, default: int | None) -> int | None:
    if key not in data:
        return default
    value = data[key]
    if value is None:
        return None
    # bool is an int subclass; a JSON `true` here is always a mistake.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{where}.{key}: expected an integer or null, got {value!r}")
    return value


def _req_int(data: Mapping[str, Any], key: str, where: str, default: int) -> int:
    value = _opt_int(data, key, where, default)
    if value is None:
        raise SpecError(f"{where}.{key}: must not be null")
    return value


def _opt_float(
    data: Mapping[str, Any], key: str, where: str, default: float | None
) -> float | None:
    if key not in data:
        return default
    value = data[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SpecError(f"{where}.{key}: expected a number or null, got {value!r}")
    return float(value)


def _req_float(data: Mapping[str, Any], key: str, where: str, default: float) -> float:
    value = _opt_float(data, key, where, default)
    if value is None:
        raise SpecError(f"{where}.{key}: must not be null")
    return value


def _req_bool(data: Mapping[str, Any], key: str, where: str, default: bool) -> bool:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise SpecError(f"{where}.{key}: expected a boolean, got {value!r}")
    return value


def _req_str(data: Mapping[str, Any], key: str, where: str, default: str | None = None) -> str:
    if key not in data:
        if default is None:
            raise SpecError(f"{where}.{key}: required")
        return default
    value = data[key]
    if not isinstance(value, str):
        raise SpecError(f"{where}.{key}: expected a string, got {value!r}")
    return value


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


# -- Limits / Guard -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Limits:
    """Two independent, individually optional stop conditions.

    ``max_depth`` is the hardware-independent ceiling (a capped-and-completed
    search is bit-identical to fixed depth on any machine); ``budget_ms`` is
    the wall-clock ceiling (hardware-dependent). The engine stops at whichever
    hits first. ``None`` = no limit on that axis; both ``None`` = "unbounded",
    where :class:`Guard.max_nodes` is the only stop.
    """

    max_depth: int | None = None
    budget_ms: float | None = None

    def __post_init__(self) -> None:
        if self.max_depth is not None and (not _is_int(self.max_depth) or self.max_depth < 1):
            raise SpecError(
                f"Limits.max_depth must be an integer >= 1 or None, got {self.max_depth!r}"
            )
        if self.budget_ms is not None:
            if not _is_number(self.budget_ms) or not self.budget_ms > 0:
                raise SpecError(
                    f"Limits.budget_ms must be a number > 0 or None, got {self.budget_ms!r}"
                )
            # Normalise so `250` and `250.0` are the same spec (same hash).
            object.__setattr__(self, "budget_ms", float(self.budget_ms))

    @property
    def unbounded(self) -> bool:
        """True when neither axis is limited (node cap is the only stop)."""
        return self.max_depth is None and self.budget_ms is None

    def to_dict(self) -> dict[str, Any]:
        return {"max_depth": self.max_depth, "budget_ms": self.budget_ms}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "limits") -> Limits:
        data = _as_mapping(data, where)
        _check_keys(data, ("max_depth", "budget_ms"), where)
        return cls(
            max_depth=_opt_int(data, "max_depth", where, None),
            budget_ms=_opt_float(data, "budget_ms", where, None),
        )


def limits_shrink_axis(lower: Limits, upper: Limits) -> str | None:
    """Name the first axis on which ``upper`` is SMALLER than ``lower``, else None.

    The ladder rule: climbing a tier may raise an axis or lift it to ``None``
    (= largest), never lower it. Equal is fine.
    """
    for axis in ("max_depth", "budget_ms"):
        lo: int | float | None = getattr(lower, axis)
        hi: int | float | None = getattr(upper, axis)
        if hi is None:
            continue  # lifting to "no limit" is the largest step up
        if lo is None or hi < lo:
            return axis
    return None


@dataclass(frozen=True, slots=True)
class Guard:
    """Always-present safety net around a search.

    ``max_nodes`` is the engine-side node cap per search -- deterministic, so
    it is what makes the both-``None`` "unbounded" mode reproducible.
    ``hang_timeout_s`` is the client watchdog (``go_engine._reply_timeout_s``):
    a hang guard that kills and respawns the engine, never a search deadline.
    """

    max_nodes: int = 50_000_000
    hang_timeout_s: float = 60.0

    def __post_init__(self) -> None:
        if not _is_int(self.max_nodes) or self.max_nodes < 1:
            raise SpecError(f"Guard.max_nodes must be an integer >= 1, got {self.max_nodes!r}")
        if not _is_number(self.hang_timeout_s) or not self.hang_timeout_s > 0:
            raise SpecError(
                f"Guard.hang_timeout_s must be a number > 0, got {self.hang_timeout_s!r}"
            )
        object.__setattr__(self, "hang_timeout_s", float(self.hang_timeout_s))

    def to_dict(self) -> dict[str, Any]:
        return {"max_nodes": self.max_nodes, "hang_timeout_s": self.hang_timeout_s}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "guard") -> Guard:
        data = _as_mapping(data, where)
        _check_keys(data, ("max_nodes", "hang_timeout_s"), where)
        defaults = cls()
        return cls(
            max_nodes=_req_int(data, "max_nodes", where, defaults.max_nodes),
            hang_timeout_s=_req_float(data, "hang_timeout_s", where, defaults.hang_timeout_s),
        )


# -- Ladder ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LadderSpec:
    """Live-only budget ladder: T1 (late game) and T2 (endgame) over T0.

    Mirrors ``LiveConfig``'s ladder knobs but each tier carries a full
    :class:`Limits` (both axes) instead of a bare budget. A tier is present
    iff its trigger is: ``late`` needs ``late_after_ply``; ``endgame`` needs
    ``endgame_when_empty_le`` (``endgame_after_ply`` is the optional
    move-count gate against the early-board empties dip; ``None`` = the runner
    inherits the T1 ply, as ``LiveConfig.ladder_endgame_after_ply`` does).
    T1 -> T2 must be non-shrinking on both axes; T0 -> first tier is checked
    by :class:`SearchSpec`, which owns T0.
    """

    late_after_ply: int | None = None
    late: Limits | None = None
    endgame_when_empty_le: int | None = None
    endgame_after_ply: int | None = None
    endgame: Limits | None = None

    def __post_init__(self) -> None:
        if (self.late is None) != (self.late_after_ply is None):
            raise SpecError("LadderSpec: `late` and `late_after_ply` must be set together")
        if (self.endgame is None) != (self.endgame_when_empty_le is None):
            raise SpecError(
                "LadderSpec: `endgame` and `endgame_when_empty_le` must be set together"
            )
        if self.endgame is None and self.endgame_after_ply is not None:
            raise SpecError("LadderSpec: `endgame_after_ply` needs an `endgame` tier")
        if self.late is None and self.endgame is None:
            raise SpecError(
                "LadderSpec: at least one tier (late / endgame) is required; "
                "use null for no ladder"
            )
        for name in ("late_after_ply", "endgame_after_ply"):
            value = getattr(self, name)
            if value is not None and (not _is_int(value) or value < 1):
                raise SpecError(f"LadderSpec.{name} must be an integer >= 1 or None, got {value!r}")
        if self.endgame_when_empty_le is not None and (
            not _is_int(self.endgame_when_empty_le) or self.endgame_when_empty_le < 0
        ):
            raise SpecError(
                "LadderSpec.endgame_when_empty_le must be an integer >= 0 or None, "
                f"got {self.endgame_when_empty_le!r}"
            )
        if self.late is not None and self.endgame is not None:
            axis = limits_shrink_axis(self.late, self.endgame)
            if axis is not None:
                raise SpecError(
                    f"LadderSpec: T2 (endgame) shrinks `{axis}` below T1 (late): "
                    f"{self.endgame} < {self.late}; the ladder must climb"
                )

    @property
    def tiers(self) -> tuple[Limits, ...]:
        """The present tiers in climbing order (T1 then T2)."""
        return tuple(t for t in (self.late, self.endgame) if t is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "late_after_ply": self.late_after_ply,
            "late": None if self.late is None else self.late.to_dict(),
            "endgame_when_empty_le": self.endgame_when_empty_le,
            "endgame_after_ply": self.endgame_after_ply,
            "endgame": None if self.endgame is None else self.endgame.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "ladder") -> LadderSpec:
        data = _as_mapping(data, where)
        _check_keys(
            data,
            ("late_after_ply", "late", "endgame_when_empty_le", "endgame_after_ply", "endgame"),
            where,
        )
        late = data.get("late")
        endgame = data.get("endgame")
        return cls(
            late_after_ply=_opt_int(data, "late_after_ply", where, None),
            late=None if late is None else Limits.from_dict(late, f"{where}.late"),
            endgame_when_empty_le=_opt_int(data, "endgame_when_empty_le", where, None),
            endgame_after_ply=_opt_int(data, "endgame_after_ply", where, None),
            endgame=None if endgame is None else Limits.from_dict(endgame, f"{where}.endgame"),
        )


# -- Search ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchSpec:
    """Everything about HOW the engine searches (as opposed to what evaluates).

    ``limits`` is T0. ``cprob`` is the base probability horizon
    (``BOT2048_CPROB_THRESH``; the budget ladder may widen it only above
    depth 8). ``workers`` is ``BOT2048_SEARCH_WORKERS``; 1 = serial =
    bit-reproducible. ``adaptive`` is the +1-depth-at-few-empties rule (Python
    expectimax; sent on the wire to Go, honoured only on the fixed-depth
    path). ``prefetch`` is a latency lever only -- decisions are unchanged.
    """

    limits: Limits = Limits()
    guard: Guard = Guard()
    cprob: float = 1e-4
    adaptive: bool = False
    p4: float = 0.1
    prefetch: bool = False
    workers: int = 1
    ladder: LadderSpec | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.limits, Limits):
            raise SpecError("SearchSpec.limits must be a Limits")
        if not isinstance(self.guard, Guard):
            raise SpecError("SearchSpec.guard must be a Guard")
        if not _is_number(self.cprob) or not 0.0 < self.cprob < 1.0:
            raise SpecError(f"SearchSpec.cprob must be in (0, 1), got {self.cprob!r}")
        if not _is_number(self.p4) or not 0.0 <= self.p4 <= 1.0:
            raise SpecError(f"SearchSpec.p4 must be in [0, 1], got {self.p4!r}")
        if not _is_int(self.workers) or self.workers < 1:
            raise SpecError(f"SearchSpec.workers must be an integer >= 1, got {self.workers!r}")
        if not isinstance(self.adaptive, bool) or not isinstance(self.prefetch, bool):
            raise SpecError("SearchSpec.adaptive / prefetch must be booleans")
        if self.ladder is not None:
            if not isinstance(self.ladder, LadderSpec):
                raise SpecError("SearchSpec.ladder must be a LadderSpec or None")
            # T0 -> first present tier: the ladder owns T1 -> T2, T0 is ours.
            first = self.ladder.tiers[0]
            axis = limits_shrink_axis(self.limits, first)
            if axis is not None:
                raise SpecError(
                    f"SearchSpec: the first ladder tier shrinks `{axis}` below T0: "
                    f"{first} < {self.limits}; the ladder must climb"
                )
        object.__setattr__(self, "cprob", float(self.cprob))
        object.__setattr__(self, "p4", float(self.p4))

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": self.limits.to_dict(),
            "guard": self.guard.to_dict(),
            "cprob": self.cprob,
            "adaptive": self.adaptive,
            "p4": self.p4,
            "prefetch": self.prefetch,
            "workers": self.workers,
            "ladder": None if self.ladder is None else self.ladder.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "search") -> SearchSpec:
        data = _as_mapping(data, where)
        _check_keys(
            data,
            ("limits", "guard", "cprob", "adaptive", "p4", "prefetch", "workers", "ladder"),
            where,
        )
        defaults = cls()
        ladder = data.get("ladder")
        return cls(
            limits=(
                defaults.limits
                if "limits" not in data
                else Limits.from_dict(data["limits"], f"{where}.limits")
            ),
            guard=(
                defaults.guard
                if "guard" not in data
                else Guard.from_dict(data["guard"], f"{where}.guard")
            ),
            cprob=_req_float(data, "cprob", where, defaults.cprob),
            adaptive=_req_bool(data, "adaptive", where, defaults.adaptive),
            p4=_req_float(data, "p4", where, defaults.p4),
            prefetch=_req_bool(data, "prefetch", where, defaults.prefetch),
            workers=_req_int(data, "workers", where, defaults.workers),
            ladder=None if ladder is None else LadderSpec.from_dict(ladder, f"{where}.ladder"),
        )


# -- Engine ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngineSpec:
    """What evaluates: which engine, which net, and its COMPLETE environment.

    ``weights`` is a bare file NAME (ntuple only), resolved by the factory
    under ``tools/ntuple/run/`` -- never a path probe, so the Mac and the
    Windows box cannot silently play different nets under one spec. ``env``
    is the environment the engine process receives (``BOT2048_*`` +
    ``GOMAXPROCS``), explicit and complete; the shell is not an input.
    """

    kind: EngineKind
    weights: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ENGINE_KINDS:
            raise SpecError(
                f"EngineSpec.kind must be one of {sorted(ENGINE_KINDS)}, got {self.kind!r}"
            )
        if self.kind == "ntuple":
            if not isinstance(self.weights, str) or not self.weights:
                raise SpecError(
                    "EngineSpec.weights is required for kind 'ntuple' (a bare file name)"
                )
            if "/" in self.weights or "\\" in self.weights or self.weights in {".", ".."}:
                raise SpecError(
                    "EngineSpec.weights must be a bare file name under tools/ntuple/run/, "
                    f"got {self.weights!r}"
                )
        elif self.weights is not None:
            raise SpecError(
                f"EngineSpec.weights is only valid for kind 'ntuple', not {self.kind!r}"
            )
        if not isinstance(self.env, Mapping):
            raise SpecError("EngineSpec.env must be a mapping of str -> str")
        for key, value in self.env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise SpecError(
                    f"EngineSpec.env entries must be str -> str, got {key!r}: {value!r}"
                )
            if key in _FORBIDDEN_ENV_KEYS:
                raise SpecError(
                    f"EngineSpec.env must not set {key}: the weight file is `weights`, "
                    "the binary is chosen from `kind`"
                )
            if not _ENV_KEY_RE.match(key):
                raise SpecError(
                    f"EngineSpec.env: {key!r} is not an engine env key (BOT2048_* or GOMAXPROCS)"
                )
        required = ENGINE_ENV_KEYS[self.kind]
        if self.kind in GO_ENGINE_KINDS:
            missing = sorted(required - set(self.env))
            if missing:
                raise SpecError(
                    f"EngineSpec.env for {self.kind!r} is incomplete; every key the engine "
                    f"reads must be pinned explicitly. Missing: {missing}"
                )
        elif self.env:
            raise SpecError(
                f"EngineSpec.env must be empty for kind {self.kind!r}, got {sorted(self.env)}"
            )
        # Freeze: a sorted read-only view, so the spec is immutable AND its
        # iteration order is canonical regardless of how it was built.
        object.__setattr__(self, "env", MappingProxyType(dict(sorted(self.env.items()))))

    # `env` is a mapping, so the dataclass-generated hash would fail at call
    # time; hash the canonical content instead (equality stays field-wise).
    def __hash__(self) -> int:
        return hash((self.kind, self.weights, tuple(self.env.items())))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "weights": self.weights, "env": dict(self.env)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "engine") -> EngineSpec:
        data = _as_mapping(data, where)
        _check_keys(data, ("kind", "weights", "env"), where)
        kind = _req_str(data, "kind", where)
        if kind not in ENGINE_KINDS:
            raise SpecError(f"{where}.kind must be one of {sorted(ENGINE_KINDS)}, got {kind!r}")
        weights = data.get("weights")
        if weights is not None and not isinstance(weights, str):
            raise SpecError(f"{where}.weights: expected a string or null, got {weights!r}")
        env_raw = data.get("env", {})
        env = _as_mapping(env_raw, f"{where}.env")
        for key, value in env.items():
            if not isinstance(value, str):
                raise SpecError(
                    f"{where}.env.{key}: values must be strings (as the engine reads them), "
                    f"got {value!r}"
                )
        # The membership check above is the Literal narrowing mypy cannot see.
        return cls(kind=cast(EngineKind, kind), weights=weights, env=dict(env))


# -- StrategySpec -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """The complete, hashable description of one strategy.

    ``farm`` selects protocol mode 1 (the farm leaf) on the Go engines.
    ``stop_exponent`` is the SIM stop rule (``11`` = classic 2048; ``None`` =
    play to game over, the record-hunt rule); live play's stop is inherent in
    the app. ``notes`` is free text and the only field outside the hash.
    """

    id: str
    engine: EngineSpec
    search: SearchSpec = SearchSpec()
    farm: bool = False
    stop_exponent: int | None = 11
    notes: str = ""
    schema: int = SPEC_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ID_RE.match(self.id):
            raise SpecError(
                f"StrategySpec.id {self.id!r} must match {_ID_RE.pattern} "
                "(lowercase name, optional +hash8 for a derived spec)"
            )
        if not isinstance(self.engine, EngineSpec):
            raise SpecError("StrategySpec.engine must be an EngineSpec")
        if not isinstance(self.search, SearchSpec):
            raise SpecError("StrategySpec.search must be a SearchSpec")
        if not isinstance(self.farm, bool):
            raise SpecError("StrategySpec.farm must be a boolean")
        if not isinstance(self.notes, str):
            raise SpecError("StrategySpec.notes must be a string")
        if self.schema != SPEC_SCHEMA:
            raise SpecError(
                f"StrategySpec.schema {self.schema!r} is not the supported schema {SPEC_SCHEMA}"
            )
        if self.stop_exponent is not None and (
            not _is_int(self.stop_exponent) or not 1 <= self.stop_exponent <= _MAX_EXPONENT
        ):
            raise SpecError(
                f"StrategySpec.stop_exponent must be an integer in [1, {_MAX_EXPONENT}] or None, "
                f"got {self.stop_exponent!r}"
            )
        kind = self.engine.kind
        if self.farm and kind not in GO_ENGINE_KINDS:
            raise SpecError(
                f"StrategySpec.farm is a Go-engine protocol mode; kind {kind!r} has none"
            )
        if kind == "trivial" and self.base_id not in TRIVIAL_STRATEGY_IDS:
            raise SpecError(
                f"kind 'trivial' dispatches on the id; {self.base_id!r} is not one of "
                f"{sorted(TRIVIAL_STRATEGY_IDS)}"
            )
        if kind in GO_ENGINE_KINDS:
            self._check_env_agrees()

    def _check_env_agrees(self) -> None:
        """``search.cprob`` / ``search.workers`` and their env twins must agree."""
        env = self.engine.env
        raw_cprob = env["BOT2048_CPROB_THRESH"]
        try:
            env_cprob = float(raw_cprob)
        except ValueError:
            raise SpecError(
                f"engine.env.BOT2048_CPROB_THRESH is not a number: {raw_cprob!r}"
            ) from None
        if env_cprob != self.search.cprob:
            raise SpecError(
                f"search.cprob ({self.search.cprob!r}) disagrees with "
                f"engine.env.BOT2048_CPROB_THRESH ({raw_cprob!r}); one truth per knob"
            )
        raw_workers = env["BOT2048_SEARCH_WORKERS"]
        if not raw_workers.isdigit() or int(raw_workers) != self.search.workers:
            raise SpecError(
                f"search.workers ({self.search.workers!r}) disagrees with "
                f"engine.env.BOT2048_SEARCH_WORKERS ({raw_workers!r}); one truth per knob"
            )

    def __hash__(self) -> int:
        return hash(self.canonical_json())

    # -- identity ------------------------------------------------------------

    @property
    def base_id(self) -> str:
        """The registry name without a derived ``+hash8`` suffix."""
        return self.id.split("+", 1)[0]

    @property
    def is_derived(self) -> bool:
        """True when the spec was produced from a registry spec by overrides."""
        return "+" in self.id

    def canonical_dict(self) -> dict[str, Any]:
        """Everything that defines the strategy -- the hashed content (no notes)."""
        d = self.to_dict()
        del d["notes"]
        return d

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @property
    def spec_hash(self) -> str:
        """sha256 of the canonical JSON, first 16 hex chars; stable across machines."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:HASH_HEX]

    @property
    def label(self) -> str:
        """``id@hash8`` -- what goes into ``Strategy.name`` / ``experiments.strategy``."""
        return f"{self.id}@{self.spec_hash[:HASH8_HEX]}"

    # -- (de)serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "id": self.id,
            "engine": self.engine.to_dict(),
            "search": self.search.to_dict(),
            "farm": self.farm,
            "stop_exponent": self.stop_exponent,
            "notes": self.notes,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        text = json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
        return text + "\n" if indent else text

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str = "spec") -> StrategySpec:
        data = _as_mapping(data, where)
        _check_keys(
            data, ("schema", "id", "engine", "search", "farm", "stop_exponent", "notes"), where
        )
        if "engine" not in data:
            raise SpecError(f"{where}.engine: required")
        defaults_search = SearchSpec()
        return cls(
            id=_req_str(data, "id", where),
            engine=EngineSpec.from_dict(data["engine"], f"{where}.engine"),
            search=(
                defaults_search
                if "search" not in data
                else SearchSpec.from_dict(data["search"], f"{where}.search")
            ),
            farm=_req_bool(data, "farm", where, False),
            stop_exponent=_opt_int(data, "stop_exponent", where, 11),
            notes=_req_str(data, "notes", where, ""),
            schema=_req_int(data, "schema", where, SPEC_SCHEMA),
        )

    @classmethod
    def from_json(cls, text: str, where: str = "spec") -> StrategySpec:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpecError(f"{where}: invalid JSON: {exc}") from exc
        return cls.from_dict(data, where)

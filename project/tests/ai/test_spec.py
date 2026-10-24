"""Tests for the declarative strategy spec (ai/spec.py) and its registry (ai/registry.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bot2048.ai.registry import (
    STRATEGIES_DIR,
    SpecNotFoundError,
    apply_overrides,
    list_specs,
    load_spec,
    parse_override,
)
from bot2048.ai.spec import (
    ENGINE2048_ENV_KEYS,
    ENGINE_ENV_KEYS,
    NTUPLE_ENV_KEYS,
    SPEC_SCHEMA,
    EngineSpec,
    Guard,
    LadderSpec,
    Limits,
    SearchSpec,
    SpecError,
    StrategySpec,
    limits_shrink_axis,
)

# -- fixtures -------------------------------------------------------------------

#: A complete engine2048 env (every key the `version` reply records) with
#: throw-away values -- the tests care about key COMPLETENESS, not values.
FARM_ENV: dict[str, str] = dict.fromkeys(sorted(ENGINE2048_ENV_KEYS), "1")
FARM_ENV["BOT2048_CPROB_THRESH"] = "0.0001"
FARM_ENV["BOT2048_SEARCH_WORKERS"] = "1"

NTUPLE_ENV: dict[str, str] = dict.fromkeys(sorted(NTUPLE_ENV_KEYS), "1")
NTUPLE_ENV["BOT2048_CPROB_THRESH"] = "0.0001"
NTUPLE_ENV["BOT2048_SEARCH_WORKERS"] = "1"


def golden_spec() -> StrategySpec:
    """The fixed spec whose hash is pinned in :func:`test_hash_golden`.

    Built in code (not loaded from the registry) so the pin only moves when the
    HASHING changes, never when a registry file is edited.
    """
    return StrategySpec(
        id="golden",
        engine=EngineSpec(kind="python"),
        search=SearchSpec(
            limits=Limits(max_depth=4, budget_ms=250),
            guard=Guard(max_nodes=1_000_000, hang_timeout_s=30),
            cprob=1e-4,
            adaptive=True,
            p4=0.1,
            prefetch=False,
            workers=1,
            ladder=LadderSpec(
                late_after_ply=2067,
                late=Limits(max_depth=6, budget_ms=2000),
                endgame_when_empty_le=3,
                endgame=Limits(max_depth=None, budget_ms=9000),
            ),
        ),
        farm=False,
        stop_exponent=11,
        notes="ignored by the hash",
    )


def farm_spec(**search: Any) -> StrategySpec:
    return StrategySpec(
        id="farm-test",
        engine=EngineSpec(kind="engine2048", env=FARM_ENV),
        search=SearchSpec(limits=Limits(max_depth=6), **search),
        farm=True,
    )


# -- hash / label ---------------------------------------------------------------


def test_hash_golden() -> None:
    # Cross-platform pin: if json/float formatting or the canonical shape ever
    # drifts (Windows vs Mac, Python minor), this literal catches it.
    spec = golden_spec()
    assert spec.spec_hash == "ade35fca75108719"
    assert spec.label == "golden@ade35fca"


def test_hash_excludes_notes_and_is_stable_over_round_trip() -> None:
    a = golden_spec()
    b = StrategySpec.from_dict({**a.to_dict(), "notes": "something else entirely"})
    assert a.spec_hash == b.spec_hash
    assert a != b  # equality is field-wise, notes included
    assert StrategySpec.from_json(a.to_json()).spec_hash == a.spec_hash
    assert "notes" not in a.canonical_dict()


def test_hash_changes_with_content() -> None:
    a = golden_spec()
    b = StrategySpec.from_dict(
        {
            **a.to_dict(),
            "search": {**a.search.to_dict(), "limits": {"max_depth": 5, "budget_ms": 250}},
        }
    )
    assert a.spec_hash != b.spec_hash
    assert len(a.spec_hash) == 16 and int(a.spec_hash, 16) >= 0


def test_int_and_float_budget_are_the_same_spec() -> None:
    assert Limits(budget_ms=250) == Limits(budget_ms=250.0)
    assert Limits(budget_ms=250).to_dict() == {"max_depth": None, "budget_ms": 250.0}


def test_specs_are_hashable_and_env_is_read_only() -> None:
    a = farm_spec()
    b = farm_spec()
    assert a == b and hash(a) == hash(b)
    assert len({a, b, golden_spec()}) == 2
    with pytest.raises(TypeError):
        a.engine.env["BOT2048_FARM_EMPTY_W"] = "0"  # type: ignore[index]
    # Env key order never leaks into the hash.
    shuffled = dict(reversed(list(FARM_ENV.items())))
    c = StrategySpec.from_dict({**a.to_dict(), "engine": {"kind": "engine2048", "env": shuffled}})
    assert c.spec_hash == a.spec_hash


# -- round trip / strict validation ------------------------------------------------


def test_round_trip_dict_and_json() -> None:
    spec = golden_spec()
    assert StrategySpec.from_dict(spec.to_dict()) == spec
    assert StrategySpec.from_json(spec.to_json()) == spec
    assert json.loads(spec.to_json())["schema"] == SPEC_SCHEMA


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.__setitem__("bogus", 1),
        lambda d: d["engine"].__setitem__("binary", "x"),
        lambda d: d["search"].__setitem__("depth", 3),
        lambda d: d["search"]["limits"].__setitem__("budget", 3),
        lambda d: d["search"]["guard"].__setitem__("nodes", 3),
        lambda d: d["search"]["ladder"].__setitem__("t3", {}),
        lambda d: d["search"]["ladder"]["late"].__setitem__("cprob", 1),
    ],
)
def test_unknown_keys_rejected_at_every_level(mutate: Any) -> None:
    data = golden_spec().to_dict()
    mutate(data)
    with pytest.raises(SpecError, match="unknown key"):
        StrategySpec.from_dict(data)


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("schema",), 2, "schema"),
        (("id",), "Has Space", "id"),
        (("id",), "with@at", "id"),
        (("farm",), "yes", "boolean"),
        (("stop_exponent",), 17, "stop_exponent"),
        (("stop_exponent",), True, "integer"),
        (("search", "cprob"), 0, "cprob"),
        (("search", "cprob"), 1, "cprob"),
        (("search", "p4"), 1.5, "p4"),
        (("search", "workers"), 0, "workers"),
        (("search", "limits", "max_depth"), 0, "max_depth"),
        (("search", "limits", "max_depth"), 2.5, "integer"),
        (("search", "limits", "budget_ms"), 0, "budget_ms"),
        (("search", "limits", "budget_ms"), "250", "number"),
        (("search", "guard", "max_nodes"), 0, "max_nodes"),
        (("search", "guard", "hang_timeout_s"), -1, "hang_timeout_s"),
        (("search", "guard", "max_nodes"), None, "null"),
        (("engine", "kind"), "cpp", "kind"),
        (("engine",), "python", "object"),
    ],
)
def test_field_validation_errors(path: tuple[str, ...], value: Any, match: str) -> None:
    data = golden_spec().to_dict()
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(SpecError, match=match):
        StrategySpec.from_dict(data)


def test_missing_engine_is_an_error_but_other_fields_default() -> None:
    with pytest.raises(SpecError, match="engine"):
        StrategySpec.from_dict({"id": "x"})
    spec = StrategySpec.from_dict({"id": "random", "engine": {"kind": "trivial"}})
    assert spec.search == SearchSpec()
    assert spec.stop_exponent == 11 and spec.farm is False and spec.notes == ""


def test_invalid_json_text() -> None:
    with pytest.raises(SpecError, match="invalid JSON"):
        StrategySpec.from_json("{not json")


# -- engine env rules -------------------------------------------------------------


def test_env_key_tables_match_the_version_replies() -> None:
    # tools/engine2048/main.go handleVersion: pool + 18 farm weights + cprob.
    assert len(ENGINE2048_ENV_KEYS) == 21
    # tools/ntuple/serve.go handleVersion: pool + 13 farm + cprob + 3 leaf
    # switches + 9 record levers (weights come from EngineSpec.weights).
    assert len(NTUPLE_ENV_KEYS) == 28
    assert "BOT2048_NTUPLE_WEIGHTS" not in NTUPLE_ENV_KEYS
    assert ENGINE_ENV_KEYS["python"] == frozenset() == ENGINE_ENV_KEYS["trivial"]


def test_go_env_must_be_complete() -> None:
    env = dict(FARM_ENV)
    del env["BOT2048_FARM_HOARD_TRIGGER"]
    with pytest.raises(SpecError, match=r"incomplete.*BOT2048_FARM_HOARD_TRIGGER"):
        EngineSpec(kind="engine2048", env=env)
    with pytest.raises(SpecError, match="incomplete"):
        EngineSpec(kind="ntuple", weights="w.bin", env={})


def test_env_extra_bot2048_key_allowed_but_foreign_key_rejected() -> None:
    EngineSpec(kind="engine2048", env={**FARM_ENV, "BOT2048_NEW_KNOB": "1"})
    with pytest.raises(SpecError, match="not an engine env key"):
        EngineSpec(kind="engine2048", env={**FARM_ENV, "PATH": "/usr/bin"})
    with pytest.raises(SpecError, match="BOT2048_NTUPLE_WEIGHTS"):
        EngineSpec(
            kind="ntuple", weights="w.bin", env={**NTUPLE_ENV, "BOT2048_NTUPLE_WEIGHTS": "x"}
        )
    with pytest.raises(SpecError, match="BOT2048_ENGINE2048_BIN"):
        EngineSpec(kind="engine2048", env={**FARM_ENV, "BOT2048_ENGINE2048_BIN": "x"})


def test_env_values_must_be_strings() -> None:
    with pytest.raises(SpecError, match="str"):
        EngineSpec(kind="engine2048", env={**FARM_ENV, "BOT2048_FARM_EMPTY_W": 340})  # type: ignore[dict-item]
    with pytest.raises(SpecError, match="values must be strings"):
        EngineSpec.from_dict(
            {"kind": "engine2048", "env": {**FARM_ENV, "BOT2048_FARM_EMPTY_W": 340}}
        )


def test_ntuple_weights_rules() -> None:
    with pytest.raises(SpecError, match="weights is required"):
        EngineSpec(kind="ntuple", env=NTUPLE_ENV)
    with pytest.raises(SpecError, match="bare file name"):
        EngineSpec(kind="ntuple", weights="run/w.bin", env=NTUPLE_ENV)
    with pytest.raises(SpecError, match="only valid for kind 'ntuple'"):
        EngineSpec(kind="engine2048", weights="w.bin", env=FARM_ENV)
    with pytest.raises(SpecError, match="only valid for kind 'ntuple'"):
        EngineSpec(kind="python", weights="w.bin")


def test_python_and_trivial_take_no_env_and_no_farm() -> None:
    with pytest.raises(SpecError, match="must be empty"):
        EngineSpec(kind="python", env={"GOMAXPROCS": "1"})
    with pytest.raises(SpecError, match="farm"):
        StrategySpec(id="py", engine=EngineSpec(kind="python"), farm=True)
    with pytest.raises(SpecError, match="dispatches on the id"):
        StrategySpec(id="clever", engine=EngineSpec(kind="trivial"))
    # A derived trivial spec keeps dispatching on the base id.
    StrategySpec(id="random+0123abcd", engine=EngineSpec(kind="trivial"))


def test_search_cprob_and_workers_must_agree_with_env() -> None:
    with pytest.raises(SpecError, match=r"cprob.*one truth"):
        farm_spec(cprob=1e-5)
    with pytest.raises(SpecError, match=r"workers.*one truth"):
        farm_spec(workers=4)
    env = {**FARM_ENV, "BOT2048_CPROB_THRESH": "abc"}
    with pytest.raises(SpecError, match="not a number"):
        StrategySpec(id="x", engine=EngineSpec(kind="engine2048", env=env), farm=True)
    # Numerically equal spellings agree.
    env = {**FARM_ENV, "BOT2048_CPROB_THRESH": "1e-4"}
    StrategySpec(id="x", engine=EngineSpec(kind="engine2048", env=env))


# -- limits / guard / ladder --------------------------------------------------------


def test_limits_unbounded_flag() -> None:
    assert Limits().unbounded
    assert not Limits(max_depth=4).unbounded
    assert not Limits(budget_ms=1).unbounded


@pytest.mark.parametrize(
    ("lower", "upper", "axis"),
    [
        (Limits(4, 250), Limits(4, 250), None),  # equal is fine
        (Limits(4, 250), Limits(6, 2000), None),  # raise both
        (Limits(4, 250), Limits(None, 250), None),  # lift depth (None = largest)
        (Limits(4, 250), Limits(4, None), None),  # lift budget
        (Limits(None, 250), Limits(None, 9000), None),  # time-only ladder
        (Limits(4, 250), Limits(3, 250), "max_depth"),  # shrink depth
        (Limits(4, 250), Limits(4, 100), "budget_ms"),  # shrink budget
        (Limits(None, 250), Limits(6, 250), "max_depth"),  # None -> number = shrink
        (Limits(4, None), Limits(4, 9000), "budget_ms"),  # None -> number = shrink
    ],
)
def test_limits_shrink_axis(lower: Limits, upper: Limits, axis: str | None) -> None:
    assert limits_shrink_axis(lower, upper) == axis


def test_ladder_t1_t2_monotonic() -> None:
    LadderSpec(
        late_after_ply=2067,
        late=Limits(6, 2000),
        endgame_when_empty_le=3,
        endgame=Limits(None, 9000),
    )
    with pytest.raises(SpecError, match=r"T2.*shrinks `budget_ms`"):
        LadderSpec(
            late_after_ply=2067,
            late=Limits(6, 2000),
            endgame_when_empty_le=3,
            endgame=Limits(None, 1000),
        )
    with pytest.raises(SpecError, match=r"T2.*shrinks `max_depth`"):
        LadderSpec(
            late_after_ply=2067,
            late=Limits(None, 2000),
            endgame_when_empty_le=3,
            endgame=Limits(8, 9000),
        )


def test_ladder_t0_vs_first_tier_monotonic() -> None:
    t0 = Limits(max_depth=4, budget_ms=250)
    SearchSpec(limits=t0, ladder=LadderSpec(late_after_ply=1, late=Limits(4, 250)))
    with pytest.raises(SpecError, match="first ladder tier shrinks `budget_ms` below T0"):
        SearchSpec(limits=t0, ladder=LadderSpec(late_after_ply=1, late=Limits(6, 100)))
    # No T1: T0 is checked against T2 directly.
    with pytest.raises(SpecError, match="shrinks `max_depth` below T0"):
        SearchSpec(
            limits=t0,
            ladder=LadderSpec(endgame_when_empty_le=3, endgame=Limits(2, 9000)),
        )


def test_ladder_tier_and_trigger_go_together() -> None:
    with pytest.raises(SpecError, match=r"late.*set together"):
        LadderSpec(late=Limits(6, 2000))
    with pytest.raises(SpecError, match=r"late.*set together"):
        LadderSpec(late_after_ply=5)
    with pytest.raises(SpecError, match=r"endgame.*set together"):
        LadderSpec(endgame=Limits(None, 9000))
    with pytest.raises(SpecError, match="endgame_after_ply"):
        LadderSpec(late_after_ply=5, late=Limits(6, 2000), endgame_after_ply=10)
    with pytest.raises(SpecError, match="at least one tier"):
        LadderSpec()
    with pytest.raises(SpecError, match="late_after_ply"):
        LadderSpec(late_after_ply=0, late=Limits(6, 2000))
    with pytest.raises(SpecError, match="endgame_when_empty_le"):
        LadderSpec(endgame_when_empty_le=-1, endgame=Limits(6, 2000))
    only_t2 = LadderSpec(endgame_when_empty_le=3, endgame_after_ply=2000, endgame=Limits(6, 2000))
    assert only_t2.tiers == (Limits(6, 2000),)


# -- registry -----------------------------------------------------------------------

REGISTRY_IDS = (
    "champion-live",
    "champion-l0003",
    "farm-baseline",
    "py-expectimax-d3",
    "random",
    "greedy",
    "corner",
)


def test_list_specs_lists_the_registry() -> None:
    assert STRATEGIES_DIR.is_dir(), STRATEGIES_DIR
    assert set(REGISTRY_IDS) <= set(list_specs())
    assert list_specs(Path("/definitely/not/here")) == ()


@pytest.mark.parametrize("name", list_specs())
def test_every_registry_file_loads_and_round_trips(name: str) -> None:
    spec = load_spec(name)
    assert spec.id == name and not spec.is_derived
    assert spec.label == f"{name}@{spec.spec_hash[:8]}"
    assert StrategySpec.from_dict(spec.to_dict()) == spec
    # The file on disk IS the canonical spec (no silent defaulting of anything
    # that plays): re-serialising and re-loading yields the same hash.
    assert StrategySpec.from_json(spec.to_json()).spec_hash == spec.spec_hash


def test_registry_labels_are_distinct() -> None:
    specs = [load_spec(n) for n in list_specs()]
    assert len({s.spec_hash for s in specs}) == len(specs)


def test_champion_live_matches_i7_production() -> None:
    # What the i7 actually ran (live DB runs.args_json, docs/production-state.md):
    # champion net + record engine, FIXED depth 4, no clock, no ladder.
    spec = load_spec("champion-live")
    assert spec.engine.kind == "ntuple"
    assert spec.engine.weights == "w-l0.003.bin"
    env = spec.engine.env
    assert env["BOT2048_RECORD_ENGINE"] == "1"
    assert env["BOT2048_EXACT_EMPTIES"] == "8"
    assert env["BOT2048_EXACT_NODES"] == "6000000"
    assert env["BOT2048_TARGET_PCONF"] == "0.6"
    assert env["BOT2048_EXACT_EARLYFIRE"] == "300"
    assert env["BOT2048_EXACT_EARLY_EMPTIES"] == "1"
    assert spec.search.limits == Limits(max_depth=4, budget_ms=None)
    assert spec.search.ladder is None
    assert spec.search.prefetch and not spec.farm
    assert set(env) == NTUPLE_ENV_KEYS


def test_champion_l0003_is_record_off_twin() -> None:
    live, base = load_spec("champion-live"), load_spec("champion-l0003")
    assert base.engine.weights == live.engine.weights == "w-l0.003.bin"
    assert base.engine.env["BOT2048_RECORD_ENGINE"] == "0"
    assert base.search == live.search
    assert base.spec_hash != live.spec_hash


def test_wall_record_differs_from_live_only_in_net() -> None:
    live, wall = load_spec("champion-live"), load_spec("wall-record")
    assert wall.engine.weights == "champion-wall-snap-0316.bin"
    assert wall.engine.env == live.engine.env
    assert wall.search == live.search


def test_farm_baseline_mirrors_experiments_baseline() -> None:
    spec = load_spec("farm-baseline")
    baseline = json.loads(
        (STRATEGIES_DIR.parent / "experiments" / "baseline.json").read_text(encoding="utf-8")
    )
    for key, value in baseline["weights"].items():
        assert float(spec.engine.env[key]) == float(value), key
    assert spec.search.limits == Limits(max_depth=6)
    assert spec.farm and not spec.search.adaptive
    assert spec.stop_exponent == 11
    assert set(spec.engine.env) == ENGINE2048_ENV_KEYS


def test_load_spec_by_path_and_errors(tmp_path: Path) -> None:
    with pytest.raises(SpecNotFoundError, match="no strategy spec"):
        load_spec("no-such-spec")
    with pytest.raises(SpecNotFoundError, match="not found"):
        load_spec(tmp_path / "missing.json")
    # By path: the id need not match the file name, derived specs are fine.
    saved = tmp_path / "anything.json"
    derived = apply_overrides(golden_spec(), {"search.limits.max_depth": 5})
    saved.write_text(derived.to_json(), encoding="utf-8")
    assert load_spec(saved) == derived
    # By name from a custom directory: the id MUST match the stem.
    liar = tmp_path / "liar.json"
    liar.write_text(golden_spec().to_json(), encoding="utf-8")
    with pytest.raises(SpecError, match="does not match the file name"):
        load_spec("liar", directory=tmp_path)
    (tmp_path / "golden.json").write_text(golden_spec().to_json(), encoding="utf-8")
    assert load_spec("golden", directory=tmp_path) == golden_spec()
    (tmp_path / "golden+01234567.json").write_text(
        StrategySpec.from_dict({**golden_spec().to_dict(), "id": "golden+01234567"}).to_json(),
        encoding="utf-8",
    )
    with pytest.raises(SpecError, match="derived"):
        load_spec("golden+01234567", directory=tmp_path)


# -- overrides --------------------------------------------------------------------------


def test_parse_override() -> None:
    assert parse_override("search.limits.budget_ms=300") == ("search.limits.budget_ms", 300)
    assert parse_override("search.limits.max_depth=null") == ("search.limits.max_depth", None)
    assert parse_override("search.prefetch=true") == ("search.prefetch", True)
    assert parse_override("engine.weights=w-l0.003.bin") == ("engine.weights", "w-l0.003.bin")
    assert parse_override('notes="a=b"') == ("notes", "a=b")
    with pytest.raises(SpecError, match=r"dotted\.path=value"):
        parse_override("search.prefetch")
    with pytest.raises(SpecError, match=r"dotted\.path=value"):
        parse_override("=1")


def test_override_yields_derived_id_never_the_base_name() -> None:
    base = golden_spec()
    derived = apply_overrides(base, ["search.limits.budget_ms=300"])
    assert derived.base_id == "golden" and derived.is_derived
    assert derived.id != base.id and derived.spec_hash != base.spec_hash
    assert derived.search.limits.budget_ms == 300.0
    # Invariant: the suffix is the content hash under the BASE id, so deriving
    # from the derived spec again lands on the same id as one step from base.
    content = StrategySpec.from_dict({**derived.to_dict(), "id": "golden"})
    assert derived.id == f"golden+{content.spec_hash[:8]}"
    twice = apply_overrides(derived, ["search.limits.budget_ms=300"])
    assert twice.id == derived.id
    # Different values -> different derived id.
    other = apply_overrides(base, ["search.limits.budget_ms=301"])
    assert other.id != derived.id


def test_override_noop_keeps_identity() -> None:
    base = golden_spec()
    assert apply_overrides(base, []) is base
    same = apply_overrides(base, {"search.limits.budget_ms": 250})
    assert same == base
    notes_only = apply_overrides(base, ["notes=changed"])
    assert notes_only.id == base.id and notes_only.notes == "changed"
    assert notes_only.spec_hash == base.spec_hash


def test_override_paths_are_validated() -> None:
    base = golden_spec()
    with pytest.raises(SpecError, match="not allowed"):
        apply_overrides(base, ["id=other"])
    with pytest.raises(SpecError, match="not allowed"):
        apply_overrides(base, ["schema=2"])
    with pytest.raises(SpecError, match="no such field 'depth'"):
        apply_overrides(base, ["search.depth=5"])
    with pytest.raises(SpecError, match="no such field 'nope'"):
        apply_overrides(base, ["nope.x=1"])
    with pytest.raises(SpecError, match="empty path segment"):
        apply_overrides(base, ["search..cprob=0.1"])
    with pytest.raises(SpecError, match="is not an object"):
        apply_overrides(base, ["search.cprob.x=1"])
    # Values still go through full validation.
    with pytest.raises(SpecError, match="budget_ms"):
        apply_overrides(base, ["search.limits.budget_ms=-1"])
    with pytest.raises(SpecError, match="shrinks"):
        apply_overrides(base, ["search.limits.budget_ms=5000"])  # T0 above T1


def test_override_whole_subtree_and_null_materialisation() -> None:
    base = golden_spec()
    flat = apply_overrides(base, ["search.ladder=null"])
    assert flat.search.ladder is None
    # Setting a child of a null subtree materialises it (then validation applies).
    rebuilt = apply_overrides(
        flat,
        {"search.ladder.late_after_ply": 2067, "search.ladder.late": {"max_depth": 6}},
    )
    assert rebuilt.search.ladder == LadderSpec(late_after_ply=2067, late=Limits(max_depth=6))
    with pytest.raises(SpecError, match="set together"):
        apply_overrides(flat, {"search.ladder.late_after_ply": 2067})


def test_override_env_values_are_coerced_to_strings() -> None:
    base = farm_spec()
    derived = apply_overrides(base, ["engine.env.BOT2048_FARM_EMPTY_W=0.5"])
    assert derived.engine.env["BOT2048_FARM_EMPTY_W"] == "0.5"
    # New BOT2048_* key is allowed (pin a knob the table does not know yet).
    pinned = apply_overrides(base, ["engine.env.BOT2048_NEW_KNOB=7"])
    assert pinned.engine.env["BOT2048_NEW_KNOB"] == "7"
    with pytest.raises(SpecError, match="not an engine env key"):
        apply_overrides(base, ["engine.env.HOME=/x"])
    with pytest.raises(SpecError, match="strings or numbers"):
        apply_overrides(base, ["engine.env.BOT2048_LEAF_CACHE=true"])
    # The env twin of search.cprob cannot be moved alone.
    with pytest.raises(SpecError, match="one truth"):
        apply_overrides(base, ["engine.env.BOT2048_CPROB_THRESH=1e-5"])
    both = apply_overrides(
        base, ["engine.env.BOT2048_CPROB_THRESH=1e-5", "search.cprob=1e-5"]
    )
    assert both.search.cprob == 1e-5


def test_load_spec_with_overrides_from_registry() -> None:
    derived = load_spec("champion-live", overrides=["search.limits.budget_ms=300"])
    assert derived.base_id == "champion-live" and derived.is_derived
    assert derived.label.startswith("champion-live+")
    assert "@" in derived.label

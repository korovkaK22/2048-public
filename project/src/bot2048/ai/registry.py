"""JSON registry of strategy specs -- ``config/strategies/<id>.json``.

One file per base spec, named after its ``id``. :func:`load_spec` accepts
either a registry name (``"champion-live"``) or a path to any ``*.json`` (an
ad-hoc spec in ``tmp/``, a derived spec someone saved). ``--set a.b=c``
style overrides go through :func:`apply_overrides`, which yields a DERIVED
spec whose id is ``<base>+<hash8>`` -- an overridden strategy is never
recorded under the base name, so two games labelled ``champion-live@...``
really did play the same thing.

Pure file + dict manipulation; nothing here starts an engine.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import structlog

from bot2048.ai.spec import (
    HASH8_HEX,
    EngineSpec,
    LadderSpec,
    Limits,
    SearchSpec,
    SpecError,
    StrategySpec,
)

log = structlog.get_logger(__name__)

__all__ = [
    "STRATEGIES_DIR",
    "SpecNotFoundError",
    "apply_overrides",
    "list_specs",
    "load_spec",
    "parse_override",
]


def _project_dir() -> Path:
    # src/bot2048/ai/registry.py -> project/  (same walk as go_engine._tools_dir)
    return Path(__file__).resolve().parents[3]


#: The registry directory: ``project/config/strategies``.
STRATEGIES_DIR: Path = _project_dir() / "config" / "strategies"

#: Top-level fields an override may never touch: the id is derived from the
#: content, the schema is fixed by the code.
_LOCKED_FIELDS: frozenset[str] = frozenset({"id", "schema"})


class SpecNotFoundError(SpecError):
    """No registry entry / file for the requested spec."""


def list_specs(directory: Path | None = None) -> tuple[str, ...]:
    """Sorted ids of every ``*.json`` in the registry directory (empty if absent)."""
    base = STRATEGIES_DIR if directory is None else directory
    if not base.is_dir():
        return ()
    return tuple(sorted(p.stem for p in base.glob("*.json") if p.is_file()))


def _resolve(name_or_path: str | Path, directory: Path) -> tuple[Path, bool]:
    """Map a name or path to a file; returns ``(path, loaded_by_name)``."""
    text = str(name_or_path)
    path = Path(name_or_path)
    looks_like_path = (
        path.suffix == ".json" or "/" in text or "\\" in text or path.is_absolute()
    )
    if looks_like_path:
        if not path.is_file():
            raise SpecNotFoundError(f"strategy spec file not found: {path}")
        return path, False
    candidate = directory / f"{text}.json"
    if not candidate.is_file():
        raise SpecNotFoundError(
            f"no strategy spec {text!r} in {directory} (known: {list(list_specs(directory))})"
        )
    return candidate, True


def load_spec(
    name_or_path: str | Path,
    *,
    directory: Path | None = None,
    overrides: Sequence[str] | Mapping[str, object] = (),
) -> StrategySpec:
    """Load a spec by registry name or file path, optionally applying overrides.

    A registry file (loaded by name) must hold a BASE spec whose ``id`` equals
    the file name, so a file can never lie about what it is. Files loaded by
    path are only required to be valid specs.
    """
    base_dir = STRATEGIES_DIR if directory is None else directory
    path, by_name = _resolve(name_or_path, base_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecNotFoundError(f"cannot read strategy spec {path}: {exc}") from exc
    spec = StrategySpec.from_json(text, where=str(path))
    if by_name:
        if spec.id != path.stem:
            raise SpecError(f"{path}: id {spec.id!r} does not match the file name {path.stem!r}")
        if spec.is_derived:
            raise SpecError(f"{path}: registry files hold base specs; {spec.id!r} is derived")
    log.debug("strategy_spec.loaded", id=spec.id, label=spec.label, path=str(path))
    if overrides:
        spec = apply_overrides(spec, overrides)
    return spec


def parse_override(text: str) -> tuple[str, object]:
    """``"search.limits.budget_ms=300"`` -> ``("search.limits.budget_ms", 300.0)``.

    The value is parsed as JSON (so ``null``, ``true``, numbers and quoted
    strings all work); anything that is not valid JSON is taken as a bare
    string (``engine.weights=w-l0.003.bin``).
    """
    key, sep, raw = text.partition("=")
    key = key.strip()
    if not sep or not key:
        raise SpecError(f"override {text!r} must look like dotted.path=value")
    try:
        value: object = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    return key, value


def _full_shape() -> dict[str, Any]:
    """A spec dict with EVERY optional subtree present -- the path vocabulary.

    Override paths are checked against this shape (not against the spec being
    overridden), so ``search.ladder.late.max_depth`` is a valid path even when
    the base spec has ``ladder: null``: the null subtree is materialised and
    then validated as a whole by ``StrategySpec.from_dict``.
    """
    return StrategySpec(
        id="shape",
        engine=EngineSpec(kind="python"),
        search=SearchSpec(
            ladder=LadderSpec(
                late_after_ply=1,
                late=Limits(),
                endgame_when_empty_le=0,
                endgame_after_ply=1,
                endgame=Limits(),
            )
        ),
    ).to_dict()


def _set_path(data: dict[str, Any], dotted: str, value: object) -> None:
    """Set ``dotted`` inside the spec dict; unknown fields are an error.

    ``engine.env.<KEY>`` may introduce a new key (that is the point of
    pinning a knob); its value is coerced to the string the engine reads.
    Everything else must be a field of the spec shape and is validated by
    ``StrategySpec.from_dict`` afterwards.
    """
    parts = dotted.split(".")
    if any(not p for p in parts):
        raise SpecError(f"override {dotted!r}: empty path segment")
    if parts[0] in _LOCKED_FIELDS:
        raise SpecError(
            f"override of {parts[0]!r} is not allowed (ids are derived, the schema is fixed)"
        )
    node: dict[str, Any] = data
    shape: dict[str, Any] = _full_shape()
    for i, part in enumerate(parts[:-1]):
        here = ".".join(parts[: i + 1])
        if part not in shape:
            raise SpecError(f"override {dotted!r}: no such field {here!r}")
        next_shape = shape[part]
        if not isinstance(next_shape, dict):
            raise SpecError(f"override {dotted!r}: {here!r} is not an object")
        shape = next_shape
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}  # materialise a null/absent subtree
        node = child
    leaf = parts[-1]
    if parts[:-1] == ["engine", "env"]:
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise SpecError(
                f"override {dotted!r}: env values must be strings or numbers, got {value!r}"
            )
        node[leaf] = value if isinstance(value, str) else str(value)
        return
    if leaf not in shape:
        raise SpecError(f"override {dotted!r}: no such field {leaf!r}")
    node[leaf] = value


def apply_overrides(
    spec: StrategySpec, overrides: Sequence[str] | Mapping[str, object]
) -> StrategySpec:
    """Return the spec with overrides applied under a derived id.

    The derived id is ``<base_id>+<hash8>`` where ``hash8`` is the content
    hash of the overridden spec computed under the BASE id (so re-deriving
    from an already derived spec lands on the same id as deriving from the
    base in one step). Overrides that leave the hashed content unchanged
    (e.g. only ``notes``) keep the original id. An empty override set returns
    the spec itself.
    """
    items: list[tuple[str, object]]
    if isinstance(overrides, Mapping):
        items = list(overrides.items())
    else:
        items = [parse_override(o) for o in overrides]
    if not items:
        return spec
    data = spec.to_dict()
    data["id"] = spec.base_id
    for path, value in items:
        _set_path(data, path, value)
    content = StrategySpec.from_dict(data, where=f"{spec.id} + overrides")
    base_content = (
        StrategySpec.from_dict({**spec.to_dict(), "id": spec.base_id}) if spec.is_derived else spec
    )
    if content.canonical_json() == base_content.canonical_json():
        # Nothing that plays changed -> same identity (notes may differ).
        return StrategySpec.from_dict({**content.to_dict(), "id": spec.id})
    derived = StrategySpec.from_dict(
        {**content.to_dict(), "id": f"{spec.base_id}+{content.spec_hash[:HASH8_HEX]}"}
    )
    log.info(
        "strategy_spec.derived",
        base=spec.id,
        id=derived.id,
        label=derived.label,
        overrides=[f"{k}={v!r}" for k, v in items],
    )
    return derived

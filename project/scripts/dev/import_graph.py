#!/usr/bin/env python
"""import_graph.py -- the real import graph between ``bot2048`` subpackages.

Walks ``src/bot2048`` with ``ast`` (nothing is imported, so it works without
the optional extras), maps every module to its top-level component
(``core``, ``ai``, ``runner``, ... and the three top-level modules
``cli``, ``logging_setup``, ``recording``) and prints every cross-component
edge with the kind of import that created it:

* ``static``  -- module-level ``import`` / ``from ... import``
* ``lazy``    -- import inside a function/method body (runtime-only edge)
* ``typing``  -- under ``if TYPE_CHECKING:`` (no runtime edge at all)

Usage (from ``project/``)::

    $PY scripts/dev/import_graph.py            # edge table
    $PY scripts/dev/import_graph.py --dot      # Graphviz source on stdout
    $PY scripts/dev/import_graph.py --json     # machine-readable

The manifest ``docs/components.md`` and the import-linter contracts in
``pyproject.toml`` were derived from this output; re-run it when a boundary
question comes up instead of guessing from the directory tree.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

PACKAGE = "bot2048"
SRC_DEFAULT = Path(__file__).resolve().parents[2] / "src" / PACKAGE


@dataclass(frozen=True, order=True)
class Edge:
    """One ``src -> dst`` import, located at ``module:line``."""

    src: str
    dst: str
    kind: str  # static | lazy | typing
    module: str
    line: int


def component_of(module: str) -> str:
    """``bot2048.runner.cli.common`` -> ``runner``; ``bot2048.cli`` -> ``cli``."""
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != PACKAGE:
        raise ValueError(f"not a {PACKAGE} module: {module}")
    return parts[1]


def module_name(path: Path, src_root: Path) -> str:
    rel = path.relative_to(src_root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from(node: ast.ImportFrom, module: str) -> str | None:
    """Absolute module path of a ``from X import Y`` (relative imports resolved)."""
    if node.level == 0:
        return node.module
    base = module.split(".")
    # ``from . import x`` inside a package __init__ counts the package itself.
    anchor = base[: len(base) - node.level + 1] if node.level <= len(base) else []
    if node.module:
        anchor = [*anchor, *node.module.split(".")]
    return ".".join(anchor) if anchor else None


class _Collector(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.edges: list[Edge] = []
        self._depth = 0  # >0 inside a function body
        self._typing = 0  # >0 inside ``if TYPE_CHECKING:``

    def _kind(self) -> str:
        if self._typing:
            return "typing"
        return "lazy" if self._depth else "static"

    def _add(self, target: str | None, line: int) -> None:
        if not target or not target.startswith(PACKAGE + "."):
            return
        src = component_of(self.module)
        try:
            dst = component_of(target)
        except ValueError:
            return
        if src != dst:
            self.edges.append(Edge(src, dst, self._kind(), self.module, line))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _resolve_from(node, self.module)
        if base is None:
            return
        # ``from bot2048 import ai`` -- the imported names are the subpackages.
        if base == PACKAGE:
            for alias in node.names:
                self._add(f"{PACKAGE}.{alias.name}", node.lineno)
            return
        self._add(base, node.lineno)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if is_tc:
            self._typing += 1
            for stmt in node.body:
                self.visit(stmt)
            self._typing -= 1
            for stmt in node.orelse:
                self.visit(stmt)
        else:
            self.generic_visit(node)


def collect(src_root: Path) -> list[Edge]:
    edges: list[Edge] = []
    for path in sorted(src_root.rglob("*.py")):
        module = module_name(path, src_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _Collector(module)
        collector.visit(tree)
        edges.extend(collector.edges)
    return edges


def components(src_root: Path) -> list[str]:
    names: set[str] = set()
    for path in src_root.iterdir():
        if path.name.startswith("_"):
            continue
        if path.is_dir() and (path / "__init__.py").exists():
            names.add(path.name)
        elif path.suffix == ".py":
            names.add(path.stem)
    return sorted(names)


def render_table(edges: list[Edge], all_components: list[str]) -> str:
    by_pair: dict[tuple[str, str], dict[str, list[Edge]]] = defaultdict(lambda: defaultdict(list))
    for e in edges:
        by_pair[(e.src, e.dst)][e.kind].append(e)

    out: list[str] = []
    out.append(f"components ({len(all_components)}): {', '.join(all_components)}")
    out.append("")
    header = f"{'from':<14} {'to':<14} {'static':>6} {'lazy':>5} {'typing':>6}"
    out.append(f"{header}  where (lazy/typing)")
    for (src, dst), kinds in sorted(by_pair.items()):
        where = ", ".join(
            f"{e.module.removeprefix(PACKAGE + '.')}:{e.line}"
            for k in ("lazy", "typing")
            for e in kinds.get(k, [])
        )
        out.append(
            f"{src:<14} {dst:<14} {len(kinds.get('static', [])):>6} "
            f"{len(kinds.get('lazy', [])):>5} {len(kinds.get('typing', [])):>6}  {where}"
        )

    out.append("")
    out.append("depends on (any kind):")
    deps: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        deps[e.src].add(e.dst)
    for c in all_components:
        out.append(f"  {c:<14} -> {', '.join(sorted(deps.get(c, ()))) or '-'}")

    out.append("")
    out.append("depended on by (any kind):")
    rdeps: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        rdeps[e.dst].add(e.src)
    for c in all_components:
        out.append(f"  {c:<14} <- {', '.join(sorted(rdeps.get(c, ()))) or '-'}")
    return "\n".join(out)


def render_dot(edges: list[Edge]) -> str:
    style = {"static": "solid", "lazy": "dashed", "typing": "dotted"}
    seen: set[tuple[str, str, str]] = set()
    lines = ["digraph bot2048 {", "  rankdir=LR;"]
    for e in sorted(edges):
        key = (e.src, e.dst, e.kind)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f'  "{e.src}" -> "{e.dst}" [style={style[e.kind]}];')
    lines.append("}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--src", type=Path, default=SRC_DEFAULT, help="path to src/bot2048")
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--dot", action="store_true", help="Graphviz source")
    fmt.add_argument("--json", action="store_true", help="JSON list of edges")
    args = parser.parse_args(argv)

    src_root: Path = args.src
    if not (src_root / "__init__.py").exists():
        print(f"error: {src_root} is not a package", file=sys.stderr)
        return 2
    edges = collect(src_root)
    if args.dot:
        print(render_dot(edges))
    elif args.json:
        print(json.dumps([asdict(e) for e in edges], indent=1))
    else:
        print(render_table(edges, components(src_root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

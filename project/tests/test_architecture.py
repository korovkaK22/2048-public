"""Component boundaries (docs/components.md) hold.

Runs the import-linter contracts declared under ``[tool.importlinter]`` in
``pyproject.toml`` against THIS checkout's ``bot2048`` (pytest puts ``src``
first on ``sys.path``, which is where grimp finds the package). A failing
contract means a module now imports across a component boundary that the
manifest says is closed -- fix the import or, if it is a conscious debt, add
an explicit ``ignore_imports`` entry with a comment next to the others.
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

importlinter_cli = pytest.importorskip(
    "importlinter.cli", reason="import-linter is in the [dev] extra; pip install -e .[dev]"
)

PROJECT = Path(__file__).resolve().parents[1]
PYPROJECT = PROJECT / "pyproject.toml"


def test_component_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    # import-linter resolves relative config paths and the grimp cache against
    # the cwd; run from project/ so the outcome does not depend on the invoker.
    monkeypatch.chdir(PROJECT)
    monkeypatch.setenv("TERM", "dumb")  # plain text in the captured report
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code: int = importlinter_cli.lint_imports(
            config_filename=str(PYPROJECT),
            no_cache=True,  # a stale cache must never hide a fresh violation
            verbose=False,
        )
    report = buf.getvalue()
    assert exit_code == 0, (
        "import-linter found a component-boundary violation "
        f"(see docs/components.md):{os.linesep}{report}"
    )
    assert "Contracts: " in report and "broken" in report

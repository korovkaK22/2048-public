"""The on-device color classifier against REAL captured screens.

Fixtures are drawn ONLY from the committed, in-tree directory
``tests/ondevice/fixtures/`` (``*.jpg`` / ``*.png``); the whole suite skips
cleanly when it is empty. This is deliberate test hygiene: the test used to
also glob ``project/logs/incidents/*/screenshot_*.png`` -- UNTRACKED
live-capture data whose contents vary per machine (SCREEN_UNEXPECTED /
VISION_MISMATCH / CRASH dumps), so the outcome depended on whichever incident
files happened to be lying around and failed non-deterministically on machines
that had run the live bot. Only committed fixtures are used now, so the result
is reproducible everywhere. Drop curated operator screenshots (settled boards,
lockscreen, the cat's paw over the top row) into ``fixtures/`` to give the
sampler contract real coverage.

The classifier under test is :mod:`bot2048.ondevice.sampler` -- the exact
algorithm the Kotlin ``BoardSampler`` ports (same crop, same paw-skip of the
top 40 % of the crop, same non-white median, same nearest-palette rule), so
this doubles as the APK classifier's fixture verification.

Contract being verified, per screen kind:

* **game board on screen** (the authoritative digits+color parser reads a
  board with no ``unknown`` cells): every cell the sampler does NOT flag
  uncertain must agree with the parser, and the uncertain-storm detector must
  stay quiet (fewer than :data:`NOT_IN_GAME_MIN_UNCERTAIN` uncertain cells);
* **not the game** (the parser finds >= 8 ``unknown`` cells: popup card,
  lockscreen, foreign screen): the sampler must raise an uncertain storm so
  the on-device ``NOT_IN_GAME`` detector latches after its streak.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot2048.ondevice.sampler import (
    NOT_IN_GAME_MIN_UNCERTAIN,
    NOT_IN_GAME_STREAK,
    NotInGameDetector,
    build_packet,
)
from bot2048.vision.calibration import Calibration
from bot2048.vision.cli import MissingImageBackendError, _load_image_rgb
from bot2048.vision.parser import parse_board_detailed

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture_paths() -> list[Path]:
    """Committed fixture images only -- never the untracked ``logs/incidents``."""
    return sorted(_FIXTURES.glob("*.jpg")) + sorted(_FIXTURES.glob("*.png"))


_PATHS = _fixture_paths()
_PARAMS = [pytest.param(p, id=p.name) for p in _PATHS] or [
    pytest.param(
        None,
        id="no-fixtures",
        marks=pytest.mark.skip(
            reason="no committed fixtures under tests/ondevice/fixtures/"
        ),
    )
]


def test_fixture_discovery_uses_only_committed_intree_fixtures() -> None:
    """Regression guard for test hygiene: discovery must draw solely from the
    committed ``fixtures/`` directory and must never reach into the untracked
    runtime ``logs/incidents/`` tree (whose contents vary per machine and made
    this suite non-deterministic)."""
    for path in _fixture_paths():
        assert _FIXTURES in path.parents, f"{path} is outside the committed fixtures dir"
        assert "incidents" not in {p.lower() for p in path.parts}, (
            f"{path} lives under a runtime logs/incidents tree"
        )


@pytest.mark.parametrize("path", _PARAMS)
def test_sampler_vs_parser_on_real_screen(path: Path, calib: Calibration) -> None:
    try:
        image = _load_image_rgb(path)
    except MissingImageBackendError as exc:  # pragma: no cover - env-dependent
        pytest.skip(str(exc))
    height, width = image.shape[0], image.shape[1]
    if (width, height) != calib.screen_size:
        pytest.skip(f"{width}x{height} does not match the calibration")

    packet = build_packet(image, calib, seq=1)
    reads = parse_board_detailed(image, calib, lenient=True)
    unknown = sum(1 for r in reads if r.method == "unknown")
    # ``color`` is the parser's own low-confidence path ("digits unreadable"):
    # a popup that dims the whole board can leave every digit unreadable while
    # the dimmed backgrounds still land NEAR palette colors, so lenient parsing
    # reports 0 unknown cells. Such a screen is NOT a clean board fixture.
    nondigit = unknown + sum(1 for r in reads if r.method == "color")
    uncertain = set(packet.uncertain_cells())

    if nondigit == 0:
        # A real board: confident sampler cells must equal the digit parser.
        disagree = [
            i
            for i, read in enumerate(reads)
            if i not in uncertain and packet.board[i] != read.exponent
        ]
        assert disagree == [], (
            f"confident sampler cells disagree with the parser at {disagree} "
            f"(sampler={packet.board}, parser={tuple(r.exponent for r in reads)})"
        )
        assert len(uncertain) < NOT_IN_GAME_MIN_UNCERTAIN, (
            f"board screen raised an uncertain storm ({sorted(uncertain)})"
        )
    elif nondigit >= NOT_IN_GAME_MIN_UNCERTAIN:
        # Not the game (popup card / lockscreen / foreign screen / dimmed
        # popup overlay): the on-device detector must latch on this evidence.
        assert len(uncertain) >= NOT_IN_GAME_MIN_UNCERTAIN, (
            f"parser saw {nondigit} non-digit cells but the sampler flagged only "
            f"{sorted(uncertain)} -- NOT_IN_GAME would never fire"
        )
        detector = NotInGameDetector()
        fired = [detector.update(packet.uncertain) for _ in range(NOT_IN_GAME_STREAK)]
        assert fired[-1] is True
    # 1..7 unknown cells (partial occlusion) has no hard contract: the packet
    # must simply be well-formed, which build_packet already guarantees.

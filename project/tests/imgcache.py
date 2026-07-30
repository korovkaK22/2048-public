"""Session cache for fixture-screenshot decoding.

Decoding one 1080x2400 reference frame costs ~23 ms. The suite reads only 20
distinct files but decoded them 138 times (~3.1 s of a ~10 s serial run):
every loader called ``_load_image_rgb`` afresh, and the ``load_image`` session
fixtures handed out a *factory* that re-decoded on each call rather than a
cached frame. Decode once per path instead, and hand out a copy -- a 7.8 MB
memcpy is ~0.1 ms, 200x cheaper than the decode -- so callers keep the freedom
to mutate their own frame in place (tests/vision/test_parser.py and
tests/vision/test_states.py do exactly that). Handing out a read-only view
would save the copy, but it would make the cache brittle against the next test
that wants to mutate a frame.

Test-side loaders only. The vision CLI keeps decoding for real: its decode
path is the code under test in tests/vision/test_vision_cli.py, and caching it
there would hollow out that test.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import numpy as np
import numpy.typing as npt

from bot2048.vision.cli import _load_image_rgb

Image = npt.NDArray[np.uint8]


@cache
def _decode_once(path: str) -> Image:
    # Keyed on str, not Path: vision/ and ondevice/ build the same fixture path
    # from different roots, and str keys collapse those to a single entry.
    return _load_image_rgb(Path(path))


def decode_fixture(path: Path) -> Image:
    """Fixture frame as RGB uint8, decoded once per session. Safe to mutate.

    Raises ``MissingImageBackendError`` (like ``_load_image_rgb``) when neither
    cv2 nor Pillow is installed; callers skip on it. Failures are not cached --
    ``functools.cache`` does not store a raising call.
    """
    return _decode_once(str(path)).copy()

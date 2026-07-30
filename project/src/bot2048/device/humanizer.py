"""Humanized swipe geometry and inter-move timing.

Two jobs:

1. Anti-detection: swipes must not be pixel-identical robotic gestures.
   Start/end points are jittered, duration and inter-move delays are
   randomized (delays are lognormal -- humans have a long right tail).

2. SAFETY (important): Android notifications slide in at the top of the
   screen, and a swipe/tap near the top edge can pull down the shade or
   open a notification. Every jittered swipe endpoint is therefore clamped
   into a *safe zone* -- the board bbox (mid-screen, roughly the 25-75%%
   vertical band on the reference 1080x2400 device) inset by
   :attr:`HumanizerConfig.safe_zone_margin_px`. Taps are NOT generated
   here at all: the bot taps only calibrated button coordinates
   (e.g. "New game"), never exploratory ones.

This module is pure math over :class:`random.Random` -- fully deterministic
under a seed and unit-tested without any hardware
(``tests/device/test_humanizer.py``).

Pacing profiles (P10)
---------------------
Input pacing is config-driven via two named profiles, selectable WITHOUT a
code change through the ``BOT2048_PACING_PROFILE`` environment variable
(``human`` | ``fast``; **default ``fast``** -- the operator wants maximum
swipe speed). The runner builds a bare ``Humanizer()``, which reads that env
var, so switching back to human is purely a config action
(``BOT2048_PACING_PROFILE=human``):

* ``fast`` -- **the default**: maximum-speed play. A fixed, jitter-free
  :data:`FAST_SWIPE_DURATION_MS` (80 ms) swipe -- the shortest gesture that
  still registers reliably on this device (``input swipe ... 60`` is the
  verified floor, docs/device-ui-notes.md; 80 ms keeps a small safety margin,
  and the live runner retries any dropped swipe) -- NO geometry or duration
  jitter, and a ZERO :meth:`Humanizer.next_delay` (``delay_median_s = 0``):
  the runner's board-settle delay + vision predict-gate already pace the
  loop, so any fixed inter-move delay here is pure dead time. Deterministic.
* ``human`` -- opt-in anti-detection: human-like gesture geometry jitter +
  lognormal inter-move delays + rare long breaks. Swipe duration jitters
  around :data:`DEFAULT_SWIPE_DURATION_MS` (300 ms).

Safety is independent of the profile: EVERY swipe endpoint (fast included) is
still clamped into the board safe zone, so the fast profile removes only the
anti-detection jitter, never the notification-shade guard.

Programmatic selection: ``HumanizerConfig.for_profile("fast")`` (or
``.from_env()``); either can be handed to ``Humanizer(config=...)``.
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import structlog

from bot2048.core.types import Direction

log = structlog.get_logger(__name__)

#: ``(left, top, right, bottom)`` in pixels, right/bottom exclusive.
BBox = tuple[int, int, int, int]

#: Human-profile swipe gesture duration (ms). 300 ms == 0.3 s -- a lifelike
#: gesture length; the ``human`` profile centres its duration jitter here.
DEFAULT_SWIPE_DURATION_MS: Final[int] = 300

#: Fast-profile swipe gesture duration (ms) -- the DEFAULT play speed. The
#: shortest gesture that still registers reliably on this device: 60 ms
#: ``input swipe`` is the verified floor (docs/device-ui-notes.md, "4/4 ходів
#: зареєстровано"), and 80 ms keeps a small safety margin over it. Any swipe
#: the game happens to drop is caught by the live runner's move_rejected
#: retry, so this is tuned for speed, not zero-loss.
FAST_SWIPE_DURATION_MS: Final[int] = 80

#: Human-profile median inter-move delay (s). Kept as ``exp(-0.6)`` (~0.549 s)
#: so the re-parameterization from the old ``delay_lognorm_mu = -0.6`` field is
#: bit-exact for the ``human`` profile.
HUMAN_DELAY_MEDIAN_S: Final[float] = math.exp(-0.6)

#: Env var that selects the pacing profile for a bare ``Humanizer()`` (the
#: runner's construction path), so the profile is switchable with zero code
#: change. Value is a :class:`PacingProfile` name; unset/invalid -> ``fast``
#: (the max-speed default); set ``human`` to restore anti-detection pacing.
PACING_ENV_VAR: Final[str] = "BOT2048_PACING_PROFILE"

#: Optional env override for the swipe gesture duration (ms) on the bare
#: ``Humanizer()`` path — the max-speed hunt knob. A positive int; sets a
#: fixed (jitter-free) duration so the value is exact. Unset/invalid ->
#: profile default (fast -> 80 ms, human -> 300 ms).
SWIPE_MS_ENV_VAR: Final[str] = "BOT2048_SWIPE_MS"


class PacingProfile(StrEnum):
    """Named input-pacing preset (swipe duration + jitter + inter-move delay)."""

    #: Human-like: geometry + duration jitter, lognormal delays, long breaks.
    HUMAN = "human"
    #: The default: deterministic, minimal-delay, shortest reliable swipe --
    #: maximum-speed play on device.
    FAST = "fast"

#: Unit vector of finger travel for each move direction.
_DIRECTION_VECTORS: Final[dict[Direction, tuple[int, int]]] = {
    Direction.UP: (0, -1),
    Direction.RIGHT: (1, 0),
    Direction.DOWN: (0, 1),
    Direction.LEFT: (-1, 0),
}


@dataclass(frozen=True, slots=True)
class SwipeParams:
    """One concrete swipe gesture in screen pixels."""

    x1: int
    y1: int
    x2: int
    y2: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class HumanizerConfig:
    """Tunable randomization parameters (defaults are sane for 1080x2400).

    Attributes:
        swipe_span_frac: finger travel as a fraction of the bbox side.
        start_jitter_px / end_jitter_px: uniform +/- jitter per coordinate.
        safe_zone_margin_px: inset of the safe zone inside the board bbox;
            all endpoints are clamped into it (notification-shade safety).
        swipe_duration_ms: base swipe gesture duration in ms (default
            :data:`DEFAULT_SWIPE_DURATION_MS` = 300).
        duration_jitter_ms: symmetric +/- jitter added to ``swipe_duration_ms``
            per swipe. ``0`` -> a fixed duration (the ``fast`` profile); the
            ``human`` profile keeps this non-zero. ``swipe_duration_ms -
            duration_jitter_ms`` must stay >= 1.
        delay_median_s: median of the lognormal inter-move delay in seconds
            (:data:`HUMAN_DELAY_MEDIAN_S` ~= 0.55 s with the human defaults).
            ``0.0`` disables the inter-move delay entirely (the ``fast``
            profile): the runner then paces purely on its board-settle
            delay + vision predict-gate.
        delay_lognorm_sigma: sigma of the underlying normal (spread of the
            lognormal). ``0`` -> a constant ``delay_median_s`` delay.
        min_delay_s: hard floor for the delay.
        long_break_prob: chance to add a long "human got distracted" pause.
        long_break_range_s: uniform range of that extra pause.

    The bare defaults ARE the ``human`` profile; :meth:`for_profile` /
    :meth:`from_env` derive the ``fast`` preset from them.
    """

    swipe_span_frac: float = 0.6
    start_jitter_px: int = 20
    end_jitter_px: int = 30
    safe_zone_margin_px: int = 8
    swipe_duration_ms: int = DEFAULT_SWIPE_DURATION_MS
    duration_jitter_ms: int = 45
    delay_median_s: float = HUMAN_DELAY_MEDIAN_S
    delay_lognorm_sigma: float = 0.45
    min_delay_s: float = 0.15
    long_break_prob: float = 0.01
    long_break_range_s: tuple[float, float] = (2.5, 9.0)

    def __post_init__(self) -> None:
        if not (0.0 < self.swipe_span_frac <= 1.0):
            raise ValueError(f"swipe_span_frac must be in (0, 1], got {self.swipe_span_frac}")
        if self.start_jitter_px < 0 or self.end_jitter_px < 0 or self.safe_zone_margin_px < 0:
            raise ValueError("jitter and margin values must be >= 0")
        if self.swipe_duration_ms <= 0:
            raise ValueError(f"swipe_duration_ms must be > 0, got {self.swipe_duration_ms}")
        if self.duration_jitter_ms < 0:
            raise ValueError(f"duration_jitter_ms must be >= 0, got {self.duration_jitter_ms}")
        if self.swipe_duration_ms - self.duration_jitter_ms < 1:
            raise ValueError(
                f"swipe_duration_ms - duration_jitter_ms must be >= 1, got "
                f"{self.swipe_duration_ms} - {self.duration_jitter_ms}"
            )
        if self.delay_median_s < 0:
            raise ValueError(f"delay_median_s must be >= 0, got {self.delay_median_s}")
        if self.delay_lognorm_sigma < 0:
            raise ValueError(f"delay_lognorm_sigma must be >= 0, got {self.delay_lognorm_sigma}")
        if self.min_delay_s < 0:
            raise ValueError(f"min_delay_s must be >= 0, got {self.min_delay_s}")
        if not (0.0 <= self.long_break_prob <= 1.0):
            raise ValueError(f"long_break_prob must be in [0, 1], got {self.long_break_prob}")
        blo, bhi = self.long_break_range_s
        if not (0.0 <= blo <= bhi):
            raise ValueError(f"invalid long_break_range_s {self.long_break_range_s!r}")

    @property
    def duration_bounds_ms(self) -> tuple[int, int]:
        """Inclusive ``(lo, hi)`` swipe-duration sampling range (``lo == hi`` when
        ``duration_jitter_ms == 0``)."""
        return (
            max(1, self.swipe_duration_ms - self.duration_jitter_ms),
            self.swipe_duration_ms + self.duration_jitter_ms,
        )

    @classmethod
    def for_profile(
        cls, profile: PacingProfile | str = PacingProfile.HUMAN, **overrides: object
    ) -> HumanizerConfig:
        """Build a config for a named :class:`PacingProfile`.

        ``human`` is the bare dataclass defaults; ``fast`` overlays the
        deterministic/minimal-delay preset. Explicit ``overrides`` win over
        the preset, so callers can tweak a single knob without abandoning the
        profile.
        """
        profile = PacingProfile(profile)
        preset = dict(_PACING_PRESETS[profile])
        preset.update(overrides)
        return cls(**preset)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HumanizerConfig:
        """Config for the profile named by :data:`PACING_ENV_VAR` (default
        ``fast`` -- max-speed play). An unknown value falls back to ``fast``
        with a warning so a typo never crashes the live loop; set ``human`` to
        restore anti-detection pacing."""
        source = env if env is not None else os.environ
        raw = source.get(PACING_ENV_VAR, PacingProfile.FAST.value).strip().lower()
        try:
            profile = PacingProfile(raw)
        except ValueError:
            log.warning(
                "humanizer.unknown_pacing_profile",
                value=raw,
                env_var=PACING_ENV_VAR,
                valid=[p.value for p in PacingProfile],
                fallback=PacingProfile.FAST.value,
            )
            profile = PacingProfile.FAST
        overrides: dict[str, object] = {}
        raw_ms = source.get(SWIPE_MS_ENV_VAR, "").strip()
        if raw_ms:
            try:
                ms = int(raw_ms)
                if ms < 1:
                    raise ValueError(raw_ms)
            except ValueError:
                log.warning(
                    "humanizer.invalid_swipe_ms",
                    value=raw_ms,
                    env_var=SWIPE_MS_ENV_VAR,
                )
            else:
                # Fixed duration: kill jitter so the env value is exact and the
                # ``swipe_duration_ms - duration_jitter_ms >= 1`` invariant holds.
                overrides["swipe_duration_ms"] = ms
                overrides["duration_jitter_ms"] = 0
        return cls.for_profile(profile, **overrides)


#: Per-profile overrides layered on the bare :class:`HumanizerConfig` defaults
#: (which are themselves the ``human`` profile). ``fast`` (the DEFAULT) is
#: deterministic: no geometry/duration jitter, a fixed
#: :data:`FAST_SWIPE_DURATION_MS` (80 ms) swipe -- the shortest reliable
#: gesture on this device -- and a ZERO inter-move delay
#: (``delay_median_s = 0``) with no long breaks. The runner's settle sleep is
#: paced purely by its ``settle_delay_s`` + vision predict-gate, which already
#: wait for the board; the previous ~30 ms constant delay here was pure dead
#: time stacked on top of every move's settle phase.
_PACING_PRESETS: Final[dict[PacingProfile, dict[str, object]]] = {
    PacingProfile.HUMAN: {},
    PacingProfile.FAST: {
        "swipe_duration_ms": FAST_SWIPE_DURATION_MS,
        "duration_jitter_ms": 0,
        "start_jitter_px": 0,
        "end_jitter_px": 0,
        "delay_median_s": 0.0,
        "delay_lognorm_sigma": 0.0,
        "min_delay_s": 0.0,
        "long_break_prob": 0.0,
    },
}


def clamp_point(x: int, y: int, zone: BBox) -> tuple[int, int]:
    """Clamp ``(x, y)`` into ``zone`` (right/bottom exclusive)."""
    left, top, right, bottom = zone
    return (min(max(x, left), right - 1), min(max(y, top), bottom - 1))


class Humanizer:
    """Stateful gesture/timing randomizer around one :class:`random.Random`.

    Pass a seeded ``random.Random`` for reproducible behavior (tests do).
    """

    def __init__(
        self,
        config: HumanizerConfig | None = None,
        rng: random.Random | None = None,
    ) -> None:
        # config is None (the runner's bare ``Humanizer()`` path) -> pick the
        # profile from BOT2048_PACING_PROFILE, so pacing is switchable with no
        # code change. An explicit config is used verbatim.
        self.config = config if config is not None else HumanizerConfig.from_env()
        self._rng = rng if rng is not None else random.Random()

    def safe_zone(self, grid_bbox: BBox) -> BBox:
        """The board bbox inset by the configured margin.

        Every generated swipe endpoint lies inside this zone, so gestures can
        never reach the status bar / notification shade at the screen top.
        """
        m = self.config.safe_zone_margin_px
        left, top, right, bottom = grid_bbox
        zone = (left + m, top + m, right - m, bottom - m)
        if zone[0] >= zone[2] or zone[1] >= zone[3]:
            raise ValueError(f"grid_bbox {grid_bbox!r} too small for margin {m}px")
        return zone

    def swipe_params(self, direction: Direction, grid_bbox: BBox) -> SwipeParams:
        """Randomized swipe for ``direction`` across the board bbox.

        The nominal gesture runs through the bbox center, spanning
        ``swipe_span_frac`` of the bbox side, then each endpoint gets an
        independent uniform jitter and is clamped into :meth:`safe_zone`.
        """
        cfg = self.config
        zone = self.safe_zone(grid_bbox)
        left, top, right, bottom = grid_bbox
        cx = (left + right) / 2
        cy = (top + bottom) / 2
        dx, dy = _DIRECTION_VECTORS[direction]
        half_x = dx * (right - left) * cfg.swipe_span_frac / 2
        half_y = dy * (bottom - top) * cfg.swipe_span_frac / 2

        sj, ej = float(cfg.start_jitter_px), float(cfg.end_jitter_px)
        x1 = round(cx - half_x + self._rng.uniform(-sj, sj))
        y1 = round(cy - half_y + self._rng.uniform(-sj, sj))
        x2 = round(cx + half_x + self._rng.uniform(-ej, ej))
        y2 = round(cy + half_y + self._rng.uniform(-ej, ej))
        x1, y1 = clamp_point(x1, y1, zone)
        x2, y2 = clamp_point(x2, y2, zone)

        lo, hi = cfg.duration_bounds_ms
        duration_ms = self._rng.randint(lo, hi)  # lo == hi -> fixed duration
        return SwipeParams(x1=x1, y1=y1, x2=x2, y2=y2, duration_ms=duration_ms)

    def next_delay(self) -> float:
        """Seconds to sleep before the next move (lognormal + rare long breaks).

        ``delay_median_s * lognormvariate(0, sigma)`` is the same distribution
        as a lognormal with ``mu = log(median)``, but a median of ``0.0`` (the
        ``fast`` profile) yields exactly ``0.0`` -- no inter-move delay, so
        fast play paces purely on the runner's settle delay + predict-gate.
        """
        cfg = self.config
        delay = cfg.delay_median_s * self._rng.lognormvariate(0.0, cfg.delay_lognorm_sigma)
        if cfg.long_break_prob > 0.0 and self._rng.random() < cfg.long_break_prob:
            delay += self._rng.uniform(*cfg.long_break_range_s)
        return max(delay, cfg.min_delay_s)

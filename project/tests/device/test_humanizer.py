"""Deterministic tests for the Humanizer (no hardware needed)."""

from __future__ import annotations

import random

import pytest

from bot2048.core.types import Direction
from bot2048.device.humanizer import (
    DEFAULT_SWIPE_DURATION_MS,
    FAST_SWIPE_DURATION_MS,
    PACING_ENV_VAR,
    SWIPE_MS_ENV_VAR,
    Humanizer,
    HumanizerConfig,
    PacingProfile,
    SwipeParams,
    clamp_point,
)

# Plausible board bbox on the reference 1080x2400 screen (mid-screen band).
BBOX = (60, 700, 1020, 1660)


def make_humanizer(seed: int = 42, **overrides: object) -> Humanizer:
    config = HumanizerConfig(**overrides)  # type: ignore[arg-type]
    return Humanizer(config=config, rng=random.Random(seed))


def test_seeded_swipes_are_reproducible() -> None:
    a = make_humanizer(seed=123)
    b = make_humanizer(seed=123)
    for direction in Direction:
        assert a.swipe_params(direction, BBOX) == b.swipe_params(direction, BBOX)


def test_different_seeds_differ() -> None:
    a = make_humanizer(seed=1).swipe_params(Direction.LEFT, BBOX)
    b = make_humanizer(seed=2).swipe_params(Direction.LEFT, BBOX)
    assert a != b


@pytest.mark.parametrize("direction", list(Direction))
def test_swipe_geometry_matches_direction(direction: Direction) -> None:
    params = make_humanizer().swipe_params(direction, BBOX)
    dx = params.x2 - params.x1
    dy = params.y2 - params.y1
    if direction is Direction.LEFT:
        assert dx < 0 and abs(dx) > abs(dy)
    elif direction is Direction.RIGHT:
        assert dx > 0 and abs(dx) > abs(dy)
    elif direction is Direction.UP:
        assert dy < 0 and abs(dy) > abs(dx)
    else:  # DOWN
        assert dy > 0 and abs(dy) > abs(dx)


def test_all_endpoints_stay_in_safe_zone() -> None:
    """Safety: jitter must never push a swipe outside the board safe zone
    (a stray gesture near the screen top could pull down the notification
    shade)."""
    hum = make_humanizer(seed=7)
    left, top, right, bottom = hum.safe_zone(BBOX)
    for _ in range(500):
        for direction in Direction:
            p = hum.swipe_params(direction, BBOX)
            for x, y in ((p.x1, p.y1), (p.x2, p.y2)):
                assert left <= x < right
                assert top <= y < bottom


def test_safe_zone_is_inset_by_margin() -> None:
    hum = make_humanizer(safe_zone_margin_px=10)
    assert hum.safe_zone(BBOX) == (70, 710, 1010, 1650)
    with pytest.raises(ValueError):
        hum.safe_zone((0, 0, 15, 15))  # too small for the margin


def test_zero_jitter_gives_nominal_centered_swipe() -> None:
    hum = make_humanizer(start_jitter_px=0, end_jitter_px=0)
    params = hum.swipe_params(Direction.LEFT, BBOX)
    cy = (700 + 1660) // 2
    assert params.y1 == cy and params.y2 == cy
    # span = 0.6 * width = 576 px, symmetric around center x=540
    assert params.x1 == 540 + 288
    assert params.x2 == 540 - 288


def test_duration_within_configured_range() -> None:
    hum = make_humanizer(swipe_duration_ms=120, duration_jitter_ms=20)
    for _ in range(200):
        params = hum.swipe_params(Direction.UP, BBOX)
        assert 100 <= params.duration_ms <= 140


def test_fixed_duration_when_jitter_zero() -> None:
    hum = make_humanizer(swipe_duration_ms=300, duration_jitter_ms=0)
    for direction in Direction:
        assert hum.swipe_params(direction, BBOX).duration_ms == 300


def test_next_delay_respects_floor_and_is_reproducible() -> None:
    a = make_humanizer(seed=99)
    b = make_humanizer(seed=99)
    delays = [a.next_delay() for _ in range(1000)]
    assert delays == [b.next_delay() for _ in range(1000)]
    assert all(d >= a.config.min_delay_s for d in delays)
    assert all(d < 120.0 for d in delays)  # sanity: no absurd sleeps


def test_long_break_probability_one_always_adds_break() -> None:
    hum = make_humanizer(long_break_prob=1.0, long_break_range_s=(5.0, 6.0))
    for _ in range(50):
        assert hum.next_delay() >= 5.0


def test_long_break_probability_zero_never_adds_break() -> None:
    hum = make_humanizer(long_break_prob=0.0, delay_median_s=0.55, delay_lognorm_sigma=0.0)
    # sigma=0 -> delay is exactly the median, well below any long break
    for _ in range(50):
        assert hum.next_delay() < 1.0


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        HumanizerConfig(swipe_span_frac=0.0)
    with pytest.raises(ValueError):
        HumanizerConfig(swipe_span_frac=1.5)
    with pytest.raises(ValueError):
        HumanizerConfig(swipe_duration_ms=0)
    with pytest.raises(ValueError):
        HumanizerConfig(duration_jitter_ms=-1)
    with pytest.raises(ValueError):
        HumanizerConfig(swipe_duration_ms=10, duration_jitter_ms=20)  # lower bound < 1
    with pytest.raises(ValueError):
        HumanizerConfig(long_break_prob=1.5)
    with pytest.raises(ValueError):
        HumanizerConfig(start_jitter_px=-1)
    with pytest.raises(ValueError):
        HumanizerConfig(delay_median_s=-0.1)


# -- pacing profiles (P10) ---------------------------------------------------------


def test_default_config_is_human_profile_300ms() -> None:
    """Bare defaults == the human profile, centred on the new 300 ms default."""
    default = HumanizerConfig()
    human = HumanizerConfig.for_profile("human")
    assert default == human == HumanizerConfig.for_profile(PacingProfile.HUMAN)
    assert default.swipe_duration_ms == DEFAULT_SWIPE_DURATION_MS == 300
    assert default.duration_jitter_ms > 0  # human keeps duration jitter
    assert default.start_jitter_px > 0 and default.end_jitter_px > 0  # + geometry jitter
    assert default.delay_median_s > 0  # + the humanizing inter-move delay


def test_fast_profile_is_deterministic_and_minimal() -> None:
    fast = HumanizerConfig.for_profile("fast")
    # fixed, short swipe (humanize OFF): no geometry/duration jitter
    assert fast.swipe_duration_ms == FAST_SWIPE_DURATION_MS
    assert fast.duration_jitter_ms == 0
    assert fast.start_jitter_px == 0 and fast.end_jitter_px == 0
    # deterministic geometry AND duration under any rng
    hum = Humanizer(config=fast, rng=random.Random(1))
    p = hum.swipe_params(Direction.LEFT, BBOX)
    assert p.duration_ms == FAST_SWIPE_DURATION_MS
    assert hum.swipe_params(Direction.LEFT, BBOX) == p  # no jitter -> identical
    # ZERO inter-move delay + never a long break. Previously fast used a
    # constant ~30 ms delay here; that landed in EVERY move's settle sleep on
    # top of settle_delay_s although the runner's predict-gate/settle-verify
    # already pace the loop, i.e. pure dead time -- so it was dropped.
    assert fast.delay_median_s == 0.0
    delays = [hum.next_delay() for _ in range(200)]
    assert delays == [0.0] * 200


def test_fast_swipe_duration_is_short_but_reliable() -> None:
    """The fast (default) swipe is in the reliable 60-120 ms band -- fast
    enough for max-speed play, above the device's verified 60 ms floor."""
    assert 60 <= FAST_SWIPE_DURATION_MS <= 120
    assert FAST_SWIPE_DURATION_MS < DEFAULT_SWIPE_DURATION_MS
    fast = HumanizerConfig.for_profile("fast")
    hum = Humanizer(config=fast, rng=random.Random(9))
    for direction in Direction:
        # every executed gesture carries the exact short duration, no jitter
        assert hum.swipe_params(direction, BBOX).duration_ms == FAST_SWIPE_DURATION_MS


def test_fast_profile_still_clamps_into_safe_zone() -> None:
    """Speed must not cost safety: even the jitter-free fast gesture stays
    inside the notification-shade-safe board zone."""
    hum = Humanizer(config=HumanizerConfig.for_profile("fast"), rng=random.Random(0))
    left, top, right, bottom = hum.safe_zone(BBOX)
    for direction in Direction:
        p = hum.swipe_params(direction, BBOX)
        for x, y in ((p.x1, p.y1), (p.x2, p.y2)):
            assert left <= x < right
            assert top <= y < bottom


def test_fast_is_the_default_profile() -> None:
    """Bare Humanizer()/from_env() default to fast (max-speed), per the
    operator's 'remove humanize, go as fast as possible' request."""
    assert HumanizerConfig.from_env({}) == HumanizerConfig.for_profile("fast")
    assert HumanizerConfig.from_env({}).swipe_duration_ms == FAST_SWIPE_DURATION_MS


def test_human_profile_keeps_delay_randomization() -> None:
    """Dropping the fast inter-move delay must NOT touch human pacing: the
    anti-detection profile still sleeps a randomized, strictly positive
    lognormal delay between moves."""
    hum = Humanizer(config=HumanizerConfig.for_profile("human"), rng=random.Random(3))
    delays = {round(hum.next_delay(), 6) for _ in range(200)}
    assert len(delays) > 50  # genuinely randomized, not constant
    assert min(delays) >= HumanizerConfig().min_delay_s > 0  # never zero


def test_for_profile_overrides_win_over_preset() -> None:
    cfg = HumanizerConfig.for_profile("fast", swipe_duration_ms=250)
    assert cfg.swipe_duration_ms == 250 and cfg.duration_jitter_ms == 0  # rest still fast


@pytest.mark.parametrize(
    # default (unset) and typos -> fast (0 jitter); only explicit human keeps jitter.
    ("value", "expected_jitter"),
    [("fast", 0), ("human", 45), ("FAST", 0), (" Human ", 45), ("bogus", 0), (None, 0)],
)
def test_from_env_selects_profile(value: str | None, expected_jitter: int) -> None:
    env = {} if value is None else {PACING_ENV_VAR: value}
    assert HumanizerConfig.from_env(env).duration_jitter_ms == expected_jitter


def test_bare_humanizer_reads_env_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner's ``Humanizer()`` (no config) must honour the env profile,
    and default to fast when it is unset."""
    monkeypatch.setenv(PACING_ENV_VAR, "human")
    assert Humanizer().config == HumanizerConfig.for_profile("human")
    monkeypatch.delenv(PACING_ENV_VAR, raising=False)
    assert Humanizer().config == HumanizerConfig.for_profile("fast")


def test_swipe_ms_env_override_sets_fixed_duration() -> None:
    """BOT2048_SWIPE_MS pins an exact, jitter-free duration on either profile."""
    cfg = HumanizerConfig.from_env({SWIPE_MS_ENV_VAR: "250"})
    assert cfg.swipe_duration_ms == 250 and cfg.duration_jitter_ms == 0
    # layered on fast, too
    fast = HumanizerConfig.from_env({PACING_ENV_VAR: "fast", SWIPE_MS_ENV_VAR: "175"})
    assert fast.swipe_duration_ms == 175 and fast.start_jitter_px == 0
    # invalid values are ignored -> profile default (fast -> 80), not a crash
    for bad in ("0", "-5", "abc", ""):
        assert (
            HumanizerConfig.from_env({SWIPE_MS_ENV_VAR: bad}).swipe_duration_ms
            == FAST_SWIPE_DURATION_MS
        )
    # explicit human profile keeps its 300 ms default when swipe_ms is invalid
    assert (
        HumanizerConfig.from_env(
            {PACING_ENV_VAR: "human", SWIPE_MS_ENV_VAR: "abc"}
        ).swipe_duration_ms
        == DEFAULT_SWIPE_DURATION_MS
    )


def test_clamp_point() -> None:
    zone = (10, 20, 110, 120)
    assert clamp_point(0, 0, zone) == (10, 20)
    assert clamp_point(500, 500, zone) == (109, 119)
    assert clamp_point(50, 60, zone) == (50, 60)


def test_swipe_params_is_frozen_value_object() -> None:
    params = SwipeParams(x1=1, y1=2, x2=3, y2=4, duration_ms=100)
    with pytest.raises(AttributeError):
        params.x1 = 5  # type: ignore[misc]

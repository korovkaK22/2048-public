"""Tests for :mod:`bot2048.runner.newgame` (in-play new-game reset, #46).

All fakes are in-memory: no adb, no real device, no network, no delays. The
fake device drives its own screen-state machine exactly like the real UI
(GAME -> tap «Нова гра» -> CONFIRM_NEW_GAME -> tap «Так, нова гра» -> fresh
GAME), and serves a scripted board on each confirm so freshness scenarios are
fully deterministic.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

import numpy as np
import numpy.typing as npt
import pytest

from bot2048.core.types import Board
from bot2048.device import ui_map
from bot2048.device.adb import EmptyScreencapError
from bot2048.runner.newgame import NewGameError, reset_to_new_game
from bot2048.vision.states import ScreenState, StateDetection

Image = npt.NDArray[np.uint8]

# A fresh two-tile board (a 2 at cell 0, a 4 at cell 15): exactly 2 tiles,
# exponents {1, 2}, value sum 2 + 4 = 6.
FRESH: Board = (1,) + (0,) * 14 + (2,)
# A mid-game board with more than two tiles (tile-count reject).
MIDGAME: Board = (3, 2, 1, 0, 2, 1, 2, 1, 1, 2, 1, 2, 2, 1, 2, 1)
# Two 4s: exactly 2 tiles, exponents {2, 2}, but value sum 4 + 4 = 8.
TWO_FOURS: Board = (2,) + (0,) * 14 + (2,)


class FakeClock:
    """Monotonic fake clock: +1.0 per call (so t0 is a distinguishable value)."""

    def __init__(self) -> None:
        self._t = 1000.0

    def __call__(self) -> float:
        self._t += 1.0
        return self._t


class FakeDevice:
    """In-memory phone + vision driving the real new-game screen flow.

    ``state`` is one of ``game`` | ``confirm`` | ``auth`` | ``off``. Tapping
    «Нова гра» in ``game`` opens the confirm popup; tapping «Так, нова гра» in
    ``confirm`` starts a game and installs the next scripted board. Every tap is
    recorded in :attr:`taps` so tests assert the exact tap sequence.
    """

    def __init__(
        self,
        *,
        initial_board: Board,
        new_boards: Iterable[Board],
        state: str = "game",
    ) -> None:
        self.board: Board = initial_board
        self._new_boards: deque[Board] = deque(new_boards)
        self.state = state
        self.taps: list[tuple[int, int]] = []
        self._frame = 0

    # -- NewGameDevice protocol ------------------------------------------------

    def screenshot(self) -> Image:
        self._frame += 1
        return np.full((4, 4, 3), self._frame % 251, dtype=np.uint8)

    def tap(self, x: int, y: int, *, sensitive: bool = False) -> None:
        self.taps.append((x, y))
        if (x, y) == ui_map.NEW_GAME and self.state == "game":
            self.state = "confirm"
        elif (x, y) == ui_map.CONFIRM_YES and self.state == "confirm":
            self.state = "game"
            if self._new_boards:
                self.board = self._new_boards.popleft()

    # -- fake vision -----------------------------------------------------------

    def detect(self, image: Image) -> StateDetection:
        mapping = {
            "game": ScreenState.GAME,
            "confirm": ScreenState.CONFIRM_NEW_GAME,
            "auth": ScreenState.AUTH_REQUIRED,
            "off": ScreenState.SCREEN_OFF,
            "unknown": ScreenState.UNKNOWN,
        }
        return StateDetection(mapping[self.state], f"fake {self.state}", 1.0)

    def read_board(self, image: Image) -> Board:
        return self.board


def _reset(phone: FakeDevice, **kwargs: object):  # type: ignore[no-untyped-def]
    return reset_to_new_game(
        phone,
        phone.detect,
        phone.read_board,
        sleep=lambda _s: None,
        clock=FakeClock(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_success_first_try() -> None:
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[FRESH])
    result = _reset(phone)

    assert result.ok
    assert bool(result) is True
    assert result.board == FRESH
    assert result.t0 is not None
    assert result.attempts == 1
    assert result.reason == "fresh"
    # tapped «Нова гра» then confirmed «Так, нова гра» -- exactly once each.
    assert phone.taps == [ui_map.NEW_GAME, ui_map.CONFIRM_YES]


def test_confirm_dialog_is_handled() -> None:
    """The «Почати нову гру?» confirm popup is detected and confirmed."""
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[FRESH])
    result = _reset(phone)

    assert result.ok
    # confirm button was tapped (the dialog was not skipped / blind-tapped).
    assert ui_map.CONFIRM_YES in phone.taps
    # confirm followed the new-game tap.
    assert phone.taps.index(ui_map.CONFIRM_YES) > phone.taps.index(ui_map.NEW_GAME)


def test_success_after_one_retry() -> None:
    """First confirm yields a stale board; the retry yields a fresh one."""
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[MIDGAME, FRESH])
    result = _reset(phone, retries=3)

    assert result.ok
    assert result.board == FRESH
    assert result.attempts == 2
    # two full tap-and-confirm cycles.
    assert phone.taps == [
        ui_map.NEW_GAME,
        ui_map.CONFIRM_YES,
        ui_map.NEW_GAME,
        ui_map.CONFIRM_YES,
    ]


def test_not_fresh_sum_too_big_then_retry_success() -> None:
    """A two-tile board whose VALUE sum exceeds the cap is rejected, then retried."""
    two_twos: Board = (1,) + (0,) * 14 + (1,)  # 2 tiles, sum 2 + 2 = 4
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[TWO_FOURS, two_twos])
    # max_start_sum=4 rejects TWO_FOURS (sum 8) via the sum branch specifically,
    # then accepts the two-2s board (sum 4).
    result = _reset(phone, retries=2, max_start_sum=4)

    assert result.ok
    assert result.board == two_twos
    assert result.attempts == 2


def test_fail_closed_when_board_never_fresh() -> None:
    """Retries exhausted without a fresh board -> ok False, no exception."""
    phone = FakeDevice(
        initial_board=MIDGAME, new_boards=[MIDGAME, MIDGAME, MIDGAME, MIDGAME]
    )
    result = _reset(phone, retries=3)

    assert not result.ok
    assert result.board is None
    assert result.t0 is None
    assert result.attempts == 4  # first try + 3 retries
    assert "tile_count" in result.reason


class _SecureAuthDevice(FakeDevice):
    """FakeDevice whose auth wall is FLAG_SECURE (#46): screencap raises
    ``EmptyScreencapError`` (0 bytes) and only ``ui_dump`` betrays the PIN pad
    -- exactly the surface behind the reset-on-start session FATAL."""

    def screenshot(self) -> Image:
        if self.state == "auth":
            raise EmptyScreencapError("screencap 0 bytes (fake FLAG_SECURE)")
        return super().screenshot()

    def ui_dump(self) -> str:
        anchor = ui_map.AUTH_TEXT_ANCHOR if self.state == "auth" else "game"
        return f"<hierarchy>{anchor}</hierarchy>"


def test_auth_wall_resolved_then_reset_proceeds() -> None:
    """An AUTH_REQUIRED probe is handed to resolve_auth (#46); once the
    delegate clears the wall, the normal tap-and-verify flow runs."""
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[FRESH], state="auth")

    def resolve(probe: object) -> bool:
        phone.state = "game"  # the delegate (AuthFlow) cleared the wall
        return True

    result = _reset(phone, resolve_auth=resolve)

    assert result.ok
    assert result.board == FRESH
    assert phone.taps == [ui_map.NEW_GAME, ui_map.CONFIRM_YES]


def test_flag_secure_auth_wall_is_classified_not_raised() -> None:
    """The FLAG_SECURE 0-byte screencap (the #46 FATAL root cause) classifies
    as the auth wall via the ui_dump fallback and is resolved --
    ``EmptyScreencapError`` never escapes the reset."""
    phone = _SecureAuthDevice(initial_board=MIDGAME, new_boards=[FRESH], state="auth")

    def resolve(probe: object) -> bool:
        phone.state = "game"
        return True

    result = _reset(phone, resolve_auth=resolve)

    assert result.ok
    assert result.board == FRESH


def test_auth_wall_unresolved_fails_closed_zero_taps() -> None:
    """resolve_auth False (auto-login off / wall persisted): NewGameError and
    zero taps into the bank UI -- fail-closed, the caller skips the reset."""
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[FRESH], state="auth")
    with pytest.raises(NewGameError):
        _reset(phone, resolve_auth=lambda _probe: False)
    assert phone.taps == []


def test_unsafe_state_raises_and_never_taps() -> None:
    """On the auth wall (or any non-GAME screen) the helper refuses to tap."""
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[FRESH], state="auth")
    with pytest.raises(NewGameError):
        _reset(phone)
    # fail-closed: zero taps into the banking app.
    assert phone.taps == []


def test_screen_off_is_unsafe() -> None:
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[FRESH], state="off")
    with pytest.raises(NewGameError):
        _reset(phone)
    assert phone.taps == []


def test_confirm_popup_already_open_is_confirmed() -> None:
    """Starting on an already-open confirm popup taps only confirm (no new-game)."""
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[FRESH], state="confirm")
    result = _reset(phone)

    assert result.ok
    assert result.board == FRESH
    assert phone.taps == [ui_map.CONFIRM_YES]


def test_timeout_returns_not_ok() -> None:
    """A zero budget on an exhausted clock stops before any fresh board."""
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[MIDGAME, MIDGAME])

    # clock advances well past a tiny deadline on the first check.
    result = reset_to_new_game(
        phone,
        phone.detect,
        phone.read_board,
        retries=5,
        timeout_s=0.0,
        sleep=lambda _s: None,
        clock=FakeClock(),
    )
    assert not result.ok
    assert result.reason == "timeout"


def test_invalid_retries_rejected() -> None:
    phone = FakeDevice(initial_board=MIDGAME, new_boards=[FRESH])
    with pytest.raises(ValueError):
        _reset(phone, retries=-1)

"""Reset the monobank 2048 clone to a FRESH game at session start (#46).

Why this exists
---------------
The in-game timer is pure wall-clock from the moment the confirm tap («Так,
нова гра») lands (docs/device-ui-notes.md, verified live 2026-07-10). To run
a clean timed session the bot must zero that timer itself by starting a brand
new game *before* the first move -- otherwise it would inherit whatever
elapsed time the game on screen already had. :func:`reset_to_new_game` performs
that "tap New game on start" and returns the monotonic ``t0`` the caller uses
as the zero of its own wall-clock mirror.

Relation to the game-over restart (#41)
----------------------------------------
:meth:`bot2048.device.supervisor.Supervisor.start_new_game` already taps
«Нова гра» -> «Так, нова гра» when a game is *over* (game-over / win popup) or
in progress, and verifies it lands on a healthy GAME screen. This helper is the
**in-play complement**: it is called while a game is ACTIVE (no popup) and adds
the piece the supervisor does not do -- a semantic **fresh-board** check (exactly
two starter tiles, only 2/4, small sum) with retries/backoff, so a session never
begins on a half-recognised or stale board. The state-branching mirrors that
supervisor flow (GAME -> tap NEW_GAME -> CONFIRM_NEW_GAME -> tap CONFIRM_YES),
reusing the same calibrated tap points from :mod:`bot2048.device.ui_map`; the
popup-restart path stays owned by the supervisor.

Fail-closed (banking app!)
--------------------------
This is a tap inside a real banking app, so the helper NEVER taps blindly: it
taps «Нова гра» only when the screen is classified :data:`ScreenState.GAME`
(or an already-open :data:`ScreenState.CONFIRM_NEW_GAME` popup), and it taps
the confirm button only when the confirm popup is actually detected. Any other
screen (auth wall, bank home, screen-off, unknown, a game-over/win popup that
belongs to the supervisor) raises :class:`NewGameError` instead of guessing --
no stray taps ever reach the bank UI.

The auth wall gets one deliberate, delegated chance first (#46): the bank
re-routes «Нова гра» through re-authorization when the session token expired,
so every probe here goes through :func:`~bot2048.runner.escalation.probe_screen`
(a FLAG_SECURE 0-byte screencap classifies as AUTH_REQUIRED instead of raising
``EmptyScreencapError`` out of the reset), and an AUTH_REQUIRED probe is handed
to the optional ``resolve_auth`` delegate -- the live loop's EXISTING opt-in
auto-login (:meth:`bot2048.runner.auth_flow.AuthFlow.resolve_reset_auth`); all
PIN handling stays inside that delegate. Unresolved auth stays fail-closed:
:class:`NewGameError`, zero taps.

Integration notes (live.py wiring)
-------------------------------------------------------
This module is self-contained and does NOT import or edit ``runner/live.py``.
To wire it in, call it once at session start (e.g. gated by a new
``LiveConfig`` flag such as ``reset_on_start``, which the caller adds to
``LiveConfig`` -- this file does not) with the objects the loop already builds:

* ``device``      -- the injected :class:`~bot2048.runner.live.LiveDevice`
  (``AdbDevice`` / the streaming capture device); only ``screenshot`` +
  ``ui_dump`` (the FLAG_SECURE-blind probe fallback) + ``tap`` are used here.
* ``detect_state``-- the loop's :class:`~bot2048.runner.live.StateDetector`
  callable (``detect_state`` bound to calibration+anchors).
* ``read_board``  -- the loop's :class:`~bot2048.runner.live.BoardReader`.

The button coordinates default to the calibrated :mod:`bot2048.device.ui_map`
points measured live on the 1080x2400 reference device
(``NEW_GAME``/``CONFIRM_YES``). They are exposed as explicit arguments so a
different device layout can be calibrated and passed without touching this
module -- **on a new device these coordinates need fresh live calibration**
(the in-play «Нова гра» button and the «Так, нова гра» confirm button).

On success the caller does the equivalent of ``LiveRunner._begin_game`` with
the returned board and ``t0`` (``t0`` being the zero of the in-game wall-clock,
captured right after the confirm tap).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

from bot2048.core.types import Board
from bot2048.device import ui_map
from bot2048.device.ui_map import Point
from bot2048.runner.escalation import ProbeResult, probe_screen
from bot2048.runner.incidents import Image
from bot2048.vision.states import ScreenState, StateDetection

log = structlog.get_logger(__name__)

__all__ = [
    "FRESH_MAX_START_EXPONENT",
    "FRESH_MAX_START_SUM",
    "FRESH_STARTER_TILES",
    "NewGameDevice",
    "NewGameError",
    "NewGameResult",
    "reset_to_new_game",
]

#: Fresh-board verification defaults (docs/device-ui-notes.md): a fresh
#: monobank-2048 board has exactly two starter tiles, each a 2 or a 4
#: (exponent <= 2), with a tile-value sum of at most 8. Module-level so the
#: live loop's resume-on-start detection (P5) applies the exact same
#: definition of "fresh" as this reset -- no drift between the two.
FRESH_STARTER_TILES: int = 2
FRESH_MAX_START_EXPONENT: int = 2
FRESH_MAX_START_SUM: int = 8


class NewGameError(RuntimeError):
    """The reset could not proceed safely (fail-closed: no blind bank taps).

    Raised when the screen is not a state this helper may act on -- e.g. the
    auth wall, bank home, an unknown/animating frame, screen-off, or a
    game-over/win popup (owned by the supervisor restart, #41). The helper
    refuses to tap rather than guess, so the exception means "a human / the
    supervisor must resolve the screen first", never "the board was not fresh"
    (that is reported as :attr:`NewGameResult.ok` ``False``).
    """


class NewGameDevice(Protocol):
    """Minimal device surface the reset needs (``AdbDevice`` satisfies it).

    A strict subset of :class:`bot2048.runner.live.LiveDevice`, so the exact
    device object the live loop already holds can be passed through unchanged.
    ``ui_dump`` is only consulted when a screencap comes back 0-byte (the
    FLAG_SECURE auth surface) -- see :func:`~bot2048.runner.escalation.probe_screen`.
    """

    def screenshot(self) -> Image: ...
    def ui_dump(self) -> str: ...
    def tap(self, x: int, y: int, *, sensitive: bool = False) -> None: ...


class StateDetector(Protocol):
    """``detect_state`` bound to calibration+anchors (see live loop)."""

    def __call__(self, image: Image) -> StateDetection: ...


class BoardReader(Protocol):
    """``parse_board`` bound to a calibration (see live loop)."""

    def __call__(self, image: Image) -> Board: ...


@dataclass(frozen=True, slots=True)
class NewGameResult:
    """Outcome of :func:`reset_to_new_game`.

    ``bool(result)`` is :attr:`ok`, so the call site can branch on the result
    directly while still reading :attr:`t0` (the wall-clock zero to mirror the
    in-game timer) and :attr:`board` (the verified fresh board) on success.

    Attributes:
        ok: True iff a fresh two-tile board was verified.
        board: the verified fresh board (``None`` on failure).
        t0: monotonic timestamp captured right after the confirm tap -- the
            zero of the in-game wall-clock timer (``None`` on failure).
        attempts: how many tap-and-verify attempts were made.
        reason: short machine-readable reason for the final outcome
            (``"fresh"`` on success; the last rejection cause otherwise).
    """

    ok: bool
    board: Board | None
    t0: float | None
    attempts: int
    reason: str

    def __bool__(self) -> bool:
        return self.ok


def _fresh_board_reason(
    board: Board,
    *,
    starter_tiles: int,
    max_start_exponent: int,
    max_start_sum: int,
) -> str | None:
    """``None`` when ``board`` looks like a fresh start; else why it does not.

    A fresh monobank-2048 board (docs/device-ui-notes.md) has exactly
    ``starter_tiles`` non-empty cells, each a 2 or a 4 (exponent 1 or 2), and a
    small tile-value sum. The tile-count and exponent checks are the real guard
    against adopting a stale mid-game board; the sum check is a cheap
    belt-and-suspenders bound ("малий сум").
    """
    tiles = [exp for exp in board if exp != 0]
    if len(tiles) != starter_tiles:
        return f"tile_count={len(tiles)}!={starter_tiles}"
    too_big = [exp for exp in tiles if exp > max_start_exponent]
    if too_big:
        return f"non_starter_exponents={sorted(too_big)}"
    if any(exp < 1 for exp in tiles):  # defensive: 0 is filtered, guards negatives
        return "non_positive_exponent"
    total = sum(1 << exp for exp in tiles)
    if total > max_start_sum:
        return f"tile_sum={total}>{max_start_sum}"
    return None


def reset_to_new_game(
    device: NewGameDevice,
    detect_state: StateDetector,
    read_board: BoardReader,
    *,
    new_game_point: Point = ui_map.NEW_GAME,
    confirm_point: Point = ui_map.CONFIRM_YES,
    retries: int = 3,
    settle_s: float = 0.8,
    backoff_s: float = 0.5,
    backoff_factor: float = 2.0,
    timeout_s: float | None = None,
    starter_tiles: int = FRESH_STARTER_TILES,
    max_start_exponent: int = FRESH_MAX_START_EXPONENT,
    max_start_sum: int = FRESH_MAX_START_SUM,
    resolve_auth: Callable[[ProbeResult], bool] | None = None,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> NewGameResult:
    """Tap «Нова гра» during an active game and verify a fresh board.

    Flow (mirrors ``Supervisor.start_new_game`` for the in-play case, then adds
    the fresh-board verification):

    1. Probe the screen via :func:`~bot2048.runner.escalation.probe_screen` (a
       FLAG_SECURE 0-byte screencap classifies as the auth wall instead of
       raising, #46); an AUTH_REQUIRED probe is first offered to
       ``resolve_auth``. Only :data:`ScreenState.GAME` (tap «Нова гра») or an
       already-open :data:`ScreenState.CONFIRM_NEW_GAME` popup (tap confirm)
       are safe start points; anything else raises :class:`NewGameError`
       (fail-closed -- never tap blindly into the bank app).
    2. On GAME: tap ``new_game_point``, settle, re-probe. If the confirm popup
       appears, tap ``confirm_point``; if the screen is already GAME again the
       app asked no confirmation; any other state raises :class:`NewGameError`.
       ``t0`` is captured right after the confirm/new-game tap.
    3. Settle, then verify the resulting board via ``read_board`` is fresh
       (:func:`_fresh_board_reason`). A non-fresh or not-yet-GAME frame is
       retried with exponential backoff, up to ``retries`` extra attempts.

    Args:
        device: screenshot + tap surface (:class:`NewGameDevice`).
        detect_state: screen classifier bound to calibration+anchors.
        read_board: board parser bound to a calibration.
        new_game_point: calibrated «Нова гра» tap point (default
            :data:`bot2048.device.ui_map.NEW_GAME`; needs live calibration on a
            new device).
        confirm_point: calibrated «Так, нова гра» confirm tap point (default
            :data:`bot2048.device.ui_map.CONFIRM_YES`).
        retries: EXTRA tap-and-verify attempts after the first (total attempts
            = ``retries + 1``).
        settle_s: pause after each tap before the next probe/read (popup +
            board animation settle).
        backoff_s: initial pause after a non-fresh attempt before retrying.
        backoff_factor: multiplier applied to the backoff after each retry.
        timeout_s: optional overall wall-clock budget (measured on ``clock``);
            ``None`` = bounded only by ``retries``.
        starter_tiles: expected number of tiles on a fresh board.
        max_start_exponent: largest allowed starter exponent (2 -> tile 4).
        max_start_sum: largest allowed sum of starter tile VALUES.
        resolve_auth: optional delegate for an AUTH_REQUIRED probe (token
            expiry re-routes «Нова гра» through re-auth, #46) -- the live loop
            passes :meth:`bot2048.runner.auth_flow.AuthFlow.resolve_reset_auth`.
            ``True`` = wall cleared (the reset re-probes and continues);
            ``False`` / not wired = fail-closed :class:`NewGameError`.
        sleep: injected sleep (tests pass a no-op).
        clock: injected monotonic clock (tests pass a fake).

    Returns:
        A :class:`NewGameResult`; ``ok`` True with a verified fresh board and
        ``t0`` on success, ``ok`` False (retries exhausted / timed out) with the
        last rejection reason otherwise.

    Raises:
        NewGameError: the screen was never a safe state to tap (fail-closed).
    """
    if retries < 0:
        raise ValueError(f"retries must be >= 0, got {retries}")
    if starter_tiles < 1:
        raise ValueError(f"starter_tiles must be >= 1, got {starter_tiles}")

    deadline = clock() + timeout_s if timeout_s is not None else None
    backoff = backoff_s
    last_reason = "not_attempted"
    attempts = 0

    for attempt in range(1, retries + 2):
        if deadline is not None and clock() > deadline:
            last_reason = "timeout"
            log.warning("newgame.timeout", attempts=attempts, last_reason=last_reason)
            break
        attempts = attempt

        t0 = _tap_new_game(
            device,
            detect_state,
            new_game_point=new_game_point,
            confirm_point=confirm_point,
            resolve_auth=resolve_auth,
            settle_s=settle_s,
            sleep=sleep,
            clock=clock,
            attempt=attempt,
        )

        sleep(settle_s)
        detection = _probe(device, detect_state, resolve_auth)
        if detection.state is not ScreenState.GAME:
            last_reason = f"post_confirm_state={detection.state.value}"
            log.info("newgame.not_settled", attempt=attempt, reason=last_reason)
            _backoff(sleep, backoff)
            backoff *= backoff_factor
            continue

        board = read_board(device.screenshot())
        reason = _fresh_board_reason(
            board,
            starter_tiles=starter_tiles,
            max_start_exponent=max_start_exponent,
            max_start_sum=max_start_sum,
        )
        if reason is None:
            log.info("newgame.fresh_board", attempt=attempt, board=board)
            return NewGameResult(
                ok=True, board=board, t0=t0, attempts=attempt, reason="fresh"
            )
        last_reason = reason
        log.info("newgame.not_fresh", attempt=attempt, reason=reason, board=board)
        _backoff(sleep, backoff)
        backoff *= backoff_factor

    log.warning("newgame.failed", attempts=attempts, reason=last_reason)
    return NewGameResult(
        ok=False, board=None, t0=None, attempts=attempts, reason=last_reason
    )


def _probe(
    device: NewGameDevice,
    detect_state: StateDetector,
    resolve_auth: Callable[[ProbeResult], bool] | None,
) -> StateDetection:
    """One auth-tolerant screen probe (the mid-game path's detection, #46).

    Uses :func:`~bot2048.runner.escalation.probe_screen` instead of a raw
    ``detect_state(device.screenshot())``: a FLAG_SECURE 0-byte screencap (the
    bank's auth surface) degrades into an AUTH_REQUIRED / DEVICE_LOST
    classification instead of raising ``EmptyScreencapError`` out of the reset
    -- the #46 session FATAL. When the wall is detected and ``resolve_auth`` is
    wired, the EXISTING auth flow gets one chance to clear it (all PIN handling
    stays inside that delegate) and the screen is re-probed; an unresolved wall
    flows back into the caller's fail-closed :class:`NewGameError` handling.
    """
    probe = probe_screen(device, detect_state)
    if probe.state is ScreenState.AUTH_REQUIRED and resolve_auth is not None:
        log.info("newgame.auth_wall", reason=probe.reason)
        if resolve_auth(probe):
            probe = probe_screen(device, detect_state)
            log.info("newgame.auth_cleared", state=probe.state.value)
    return StateDetection(probe.state, probe.reason, probe.board_match_frac)


def _tap_new_game(
    device: NewGameDevice,
    detect_state: StateDetector,
    *,
    new_game_point: Point,
    confirm_point: Point,
    resolve_auth: Callable[[ProbeResult], bool] | None,
    settle_s: float,
    sleep: Callable[[float], object],
    clock: Callable[[], float],
    attempt: int,
) -> float:
    """Perform ONE «Нова гра» (-> confirm) tap sequence; return the timer zero.

    Fail-closed: taps «Нова гра» only from :data:`ScreenState.GAME`, taps the
    confirm button only when :data:`ScreenState.CONFIRM_NEW_GAME` is detected,
    and raises :class:`NewGameError` for every other screen -- no blind taps.
    The returned ``t0`` is ``clock()`` right after the confirm (or the
    new-game tap when the app asked no confirmation), i.e. the wall-clock zero
    of the in-game timer. Probes go through :func:`_probe`, so an auth wall
    met before or right after the «Нова гра» tap is offered to ``resolve_auth``
    first (#46) -- only an unresolved wall raises.
    """
    detection = _probe(device, detect_state, resolve_auth)
    state = detection.state

    if state is ScreenState.CONFIRM_NEW_GAME:
        # Confirm popup already open (a previous attempt died between taps).
        log.info("newgame.confirm_already_open", attempt=attempt)
        device.tap(*confirm_point)
        return clock()

    if state is not ScreenState.GAME:
        # Auth wall, bank home, screen-off, unknown, or a game-over/win popup
        # (owned by the supervisor restart) -- refuse to tap into the bank app.
        log.warning("newgame.unsafe_state", attempt=attempt, state=state.value)
        raise NewGameError(
            f"refusing to tap «Нова гра»: unsafe screen state {state.value}"
        )

    log.info("newgame.tap_new_game", attempt=attempt, point=new_game_point)
    device.tap(*new_game_point)
    sleep(settle_s)

    after = _probe(device, detect_state, resolve_auth)
    if after.state is ScreenState.CONFIRM_NEW_GAME:
        log.info("newgame.tap_confirm", attempt=attempt, point=confirm_point)
        device.tap(*confirm_point)
        return clock()  # game-timer zero: wall-clock from this instant
    if after.state is ScreenState.GAME:
        # No confirmation asked (nothing in progress to lose).
        log.info("newgame.no_confirm_dialog", attempt=attempt)
        return clock()

    # Something else came up after the «Нова гра» tap -- do NOT tap confirm blindly.
    log.warning(
        "newgame.unexpected_after_tap", attempt=attempt, state=after.state.value
    )
    raise NewGameError(
        f"after «Нова гра» tap, unexpected screen state {after.state.value}"
    )


def _backoff(sleep: Callable[[float], object], seconds: float) -> None:
    if seconds > 0:
        sleep(seconds)

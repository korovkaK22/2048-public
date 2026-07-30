"""Calibrated UI tap points for the monobank 2048 clone (1080x2400).

Every coordinate below was MEASURED on the reference device -- either from
annotated frames (``tmp/ng1_dialog.png``: the "start a new game?" popup) or
live experiments logged in docs/device-ui-notes.md. Taps anywhere else are
forbidden (banking app; a stray tap near the screen top opens the
notification shade).

New-game flow (docs/device-ui-notes.md):
    tap NEW_GAME -> CONFIRM_NEW_GAME popup -> tap CONFIRM_YES (fresh board)
    or CONTINUE_GAME / POPUP_CLOSE to keep the current one.

PIN pad (AUTH_REQUIRED screen, «Уведіть ПІН-код»): a fixed 3x4 grid. The
user DISABLED biometrics (2026-07-10), so the auth screen is deterministic:
always the pin pad, no fingerprint/BiometricPrompt sheet to dismiss. The
PIN itself NEVER appears in code, config, logs or the DB -- it is read from
an env var at call time (see :mod:`bot2048.device.auth`).

Entering the game: ``am start`` into ``Game2028Activity`` is FORBIDDEN (the
activity is not exported). The game is reached only through the app UI or
the task stack -- after auth the focus returns to the game by itself
(verified live).
"""

from __future__ import annotations

from typing import Final

Point = tuple[int, int]

#: "Нова гра" button, bottom-center of the normal game screen.
NEW_GAME: Final[Point] = (540, 2143)

#: Blue "Так, нова гра" button on the confirmation popup.
CONFIRM_YES: Final[Point] = (540, 1532)

#: "Продовжити поточну гру" text button on the confirmation popup.
CONTINUE_GAME: Final[Point] = (540, 1682)

#: «Нова гра» button shared by the «Ви зібрали 2048!» win popup
#: (WIN_POPUP_2048) and the «Гру завершено» game-over popup (GAME_OVER_POPUP).
#: Tapping it starts a NEW game directly (no confirmation -- the finished game
#: is already over). ONE point must land on the button in THREE layouts, whose
#: button rects sit at different rows (docs/device-ui-notes.md):
#:   * win popup            -- y ~1520..1766 (orange button)
#:   * game-over SHORT card -- y ~1565..1765 («Нова гра» only)
#:   * game-over TALL card  -- y ~1489..1632 (extra «Вийти» pushes it up)
#: (540, 1600) is the OVERLAP of all three, so it hits «Нова гра» in every
#: case and stays well clear of the tall card's «Вийти» (y~1710). NOTE: the
#: previous value (540, 1638) sat in the tall card's DEAD GAP below the button
#: -- that is exactly why incident 20260711T105055Z (score 29 892) would have
#: failed to restart once its game-over popup was finally detected.
WIN_NEW_GAME: Final[Point] = (540, 1600)

#: White "x" that closes the confirmation popup.
POPUP_CLOSE: Final[Point] = (173, 718)

#: PIN pad digits on the auth screen (fixed layout, measured live).
PIN_KEYS: Final[dict[str, Point]] = {
    "1": (258, 1159),
    "2": (541, 1159),
    "3": (823, 1159),
    "4": (258, 1420),
    "5": (541, 1420),
    "6": (823, 1420),
    "7": (258, 1681),
    "8": (541, 1681),
    "9": (823, 1681),
    "0": (541, 1942),
}

#: Backspace key on the PIN pad.
PIN_BACKSPACE: Final[Point] = (823, 1942)

#: uiautomator-dump text anchor of the auth screen (package com.ftband.mono).
AUTH_TEXT_ANCHOR: Final[str] = "Уведіть ПІН-код"

__all__ = [
    "AUTH_TEXT_ANCHOR",
    "CONFIRM_YES",
    "CONTINUE_GAME",
    "NEW_GAME",
    "PIN_BACKSPACE",
    "PIN_KEYS",
    "POPUP_CLOSE",
    "WIN_NEW_GAME",
    "Point",
]

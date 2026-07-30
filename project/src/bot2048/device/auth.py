"""PIN entry for the monobank auth wall (AUTH_REQUIRED).

SECURITY CONTRACT (do not weaken):

* The PIN is read ONLY from an environment variable at call time (default
  ``MONO_PIN``). The variable is populated by the launcher from
  ``.secrets/device.env`` (file to be created only when the user hands over
  the PIN). While the PIN is ABSENT, auto-login must fail cleanly:
  :func:`read_pin` returns ``None`` and the supervisor escalates
  AUTH_REQUIRED as FATAL («потрібен користувач») -- no taps, no guessing.
* The PIN never appears in code, config files, the DB, CLI arguments,
  incident files or logs -- log lines never include digits.
* Auto-login is OPT-IN; the default policy (docs/ops-monitoring.md §5) is
  AUTH_REQUIRED -> immediate FATAL and a human unlocks the phone.
* Taps are issued only at the calibrated pin-pad points in
  :mod:`bot2048.device.ui_map` -- no exploration on the auth screen.

Auth screen («Уведіть ПІН-код», docs/device-ui-notes.md): the user DISABLED
biometrics (2026-07-10), so the screen is deterministic -- always the bare
pin pad, no fingerprint/BiometricPrompt sheet. We type the digits with
humanized delays. Verification (did we land back in the game?) is the
supervisor's job -- screencap is blind here (FLAG_SECURE) until the wall is
gone; after auth the focus returns to the game by itself (task stack).
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from typing import Any, Protocol

import structlog

from bot2048.device import ui_map

log = structlog.get_logger(__name__)

__all__ = ["AuthError", "TapDevice", "enter_pin", "read_pin"]

#: Delay bounds between pin-pad taps (humanized, seconds).
_KEY_DELAY_RANGE_S: tuple[float, float] = (0.25, 0.55)


class AuthError(RuntimeError):
    """PIN is unusable (bad format / wrong var). NEVER contains the value."""


class TapDevice(Protocol):
    """The only device capability auth needs: calibrated taps.

    ``sensitive`` keeps the tap coordinates out of the debug log; PIN entry
    passes it, because the keypad layout is public and logged coordinates would
    reconstruct the PIN.
    """

    def tap(self, x: int, y: int, *, sensitive: bool = False) -> None: ...


def read_pin(env_var: str = "MONO_PIN") -> str | None:
    """Read the PIN from ``env_var``; ``None`` when unset/empty.

    The launcher sources ``.secrets/device.env`` into the environment; that
    file does not exist until the user provides the PIN, so ``None`` is the
    normal "no PIN available" answer and the caller must fall back to the
    AUTH_REQUIRED FATAL path (wait for the human).

    Raises:
        AuthError: set but not 4-8 digits. The message deliberately does
            not echo the value.
    """
    value = os.environ.get(env_var)
    if value is None or value == "":
        return None
    # isascii() matters as much as isdigit(): str.isdigit() accepts Unicode
    # digits ("٣", "²", ...), which would pass validation and then miss
    # ui_map.PIN_KEYS, raising a KeyError that CARRIES THE CHARACTER into the
    # unhandled-exception path (alerts.jsonl -> daily log -> Telegram).
    if not (value.isascii() and value.isdigit()) or not 4 <= len(value) <= 8:
        raise AuthError(
            f"env var {env_var} must be 4-8 ASCII digits (value not shown); "
            "fix the variable or disable auto_login"
        )
    return value


def enter_pin(
    device: TapDevice,
    pin: str,
    *,
    sleep: Callable[[float], Any] = time.sleep,
    rng: random.Random | None = None,
) -> None:
    """Type ``pin`` on the calibrated pad (deterministic pin-pad screen).

    Does NOT verify success -- the caller (supervisor) re-acquires a frame
    afterwards. Never logs digits.
    """
    rng = rng or random.Random()
    lo, hi = _KEY_DELAY_RANGE_S
    log.info("auth.pin_entry_started")  # no digits, no length
    for digit in pin:
        try:
            x, y = ui_map.PIN_KEYS[digit]
        except KeyError:
            # Never let the KeyError escape: its message would be the digit
            # itself, and an unhandled exception here is forwarded outward.
            raise AuthError("PIN contains an unmapped key (value not shown)") from None
        # sensitive=True: the keypad layout is public, so logging coordinates
        # here would reconstruct the PIN at DEBUG level.
        device.tap(x, y, sensitive=True)
        sleep(rng.uniform(lo, hi))
    log.info("auth.pin_entry_finished")

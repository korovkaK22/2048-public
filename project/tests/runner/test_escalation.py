"""``probe_screen`` liveness: a FLAG_SECURE-blind screencap must never be
mislabeled ``DEVICE_LOST`` (feat/fix-false-device-lost).

Live incident: ``screencap 0 bytes and ui_dump failed: uiautomator dump
produced no hierarchy (got 59 chars)`` was returned as ``device_lost`` while
the APK board stream kept flowing -- the device was NOT lost; the screen was
the bank's FLAG_SECURE auth/PIN surface (its documented signature is exactly
a 0-byte screencap, docs/device-ui-notes.md), and ``uiautomator dump`` is
known to yield no hierarchy during transitions.

SAFETY: ``AUTH_REQUIRED`` is ``fatal_immediately`` (zero taps on the bank);
``DEVICE_LOST`` is a retry/recovery path. Mislabeling the auth surface as
``DEVICE_LOST`` therefore routes the bank's auth wall toward recovery
handling -- the exact thing the "no taps on the bank" rule forbids. These
tests pin the fixed contract:

* screencap-0 + blank/failed ``ui_dump`` + APK frames FLOWING -> a non-lost,
  non-tap state (``SECURE_UNCONFIRMED`` / ``AUTH_REQUIRED``), never
  ``DEVICE_LOST``;
* ``DEVICE_LOST`` from that branch stays reachable ONLY when the APK frame
  stream has ALSO stopped (screencap AND ui_dump AND frames all dead);
* the auth-wall path stays ``fatal_immediately`` and is never downgraded.

Device and APK frame source are mocks -- no adb, no real sleeps.
"""

from __future__ import annotations

import pytest

from bot2048.device import ui_map
from bot2048.device.adb import DeviceError, EmptyScreencapError
from bot2048.device.supervisor import Supervisor
from bot2048.runner.alerts import AlertCode
from bot2048.runner.escalation import (
    POLICIES,
    SECURE_UI_DUMP_RETRIES,
    confirm_incident,
    probe_screen,
)
from bot2048.vision.states import ScreenState

AUTH_XML = f"<hierarchy><node text='{ui_map.AUTH_TEXT_ANCHOR}'/></hierarchy>"

#: 1 initial attempt + the retries probe_screen performs by default.
DUMP_ATTEMPTS = 1 + SECURE_UI_DUMP_RETRIES


class FakeProbeDevice:
    """Scripted ProbeDevice: ``screenshot`` raises, ``ui_dump`` plays a list.

    ``ui_dumps`` items are either an XML string (returned) or an exception
    (raised); an exhausted list keeps raising ``DeviceError`` (the live
    failure mode: no hierarchy, over and over).
    """

    def __init__(
        self,
        *,
        screenshot_exc: Exception,
        ui_dumps: list[object] | None = None,
    ) -> None:
        self._screenshot_exc = screenshot_exc
        self._ui_dumps = list(ui_dumps or [])
        self.ui_dump_calls = 0

    def screenshot(self):
        raise self._screenshot_exc

    def ui_dump(self) -> str:
        self.ui_dump_calls += 1
        if not self._ui_dumps:
            raise DeviceError("uiautomator dump produced no hierarchy (got 59 chars)")
        item = self._ui_dumps.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, str)
        return item


def _never_classify(image):
    raise AssertionError("classify must not run when no screenshot exists")


def _no_sleep(recorded: list[float]):
    def _sleep(seconds: float) -> None:
        recorded.append(seconds)

    return _sleep


# -- (a) screencap-0 + blank ui_dump + frames FLOWING -> never DEVICE_LOST ----


def test_secure_blind_with_frames_flowing_is_not_device_lost() -> None:
    """The incident scenario: blind screencap, no hierarchy, packets flowing."""
    device = FakeProbeDevice(screenshot_exc=EmptyScreencapError("0 bytes"))
    sleeps: list[float] = []

    result = probe_screen(
        device,
        _never_classify,
        frames_flowing=lambda: True,
        sleep=_no_sleep(sleeps),
    )

    assert result.state is not ScreenState.DEVICE_LOST
    assert result.state is ScreenState.SECURE_UNCONFIRMED
    assert not result.healthy  # still escalates -- just never as "lost"
    # ui_dump was retried (transition blanks are common), with waits between.
    assert device.ui_dump_calls == DUMP_ATTEMPTS
    assert len(sleeps) == DUMP_ATTEMPTS - 1


def test_secure_blind_without_apk_source_is_not_device_lost() -> None:
    """No APK source wired: screencap ANSWERED (0 bytes), so still not lost."""
    device = FakeProbeDevice(screenshot_exc=EmptyScreencapError("0 bytes"))

    result = probe_screen(
        device, _never_classify, frames_flowing=None, sleep=_no_sleep([])
    )

    assert result.state is ScreenState.SECURE_UNCONFIRMED
    assert result.state is not ScreenState.DEVICE_LOST


def test_auth_anchor_found_on_retry_wins_over_everything() -> None:
    """A blank first dump then the PIN anchor -> AUTH_REQUIRED (the truth)."""
    device = FakeProbeDevice(
        screenshot_exc=EmptyScreencapError("0 bytes"),
        ui_dumps=[DeviceError("no hierarchy"), AUTH_XML],
    )

    result = probe_screen(
        device, _never_classify, frames_flowing=lambda: True, sleep=_no_sleep([])
    )

    assert result.state is ScreenState.AUTH_REQUIRED
    assert result.uidump_xml == AUTH_XML
    assert device.ui_dump_calls == 2


def test_nonblank_dump_without_anchor_stays_unknown() -> None:
    """A real hierarchy without the anchor keeps the old UNKNOWN verdict,
    even when frames have stopped -- the dump itself proves the link works."""
    device = FakeProbeDevice(
        screenshot_exc=EmptyScreencapError("0 bytes"),
        ui_dumps=["<hierarchy><node text='something'/></hierarchy>"],
    )

    result = probe_screen(
        device, _never_classify, frames_flowing=lambda: False, sleep=_no_sleep([])
    )

    assert result.state is ScreenState.UNKNOWN
    assert device.ui_dump_calls == 1


def test_empty_string_dump_counts_as_blank_and_is_retried() -> None:
    device = FakeProbeDevice(
        screenshot_exc=EmptyScreencapError("0 bytes"),
        ui_dumps=["   \n", AUTH_XML],
    )

    result = probe_screen(
        device, _never_classify, frames_flowing=lambda: True, sleep=_no_sleep([])
    )

    assert result.state is ScreenState.AUTH_REQUIRED
    assert device.ui_dump_calls == 2


# -- (b) real disconnect stays reachable --------------------------------------


def test_secure_blind_with_frames_stopped_is_still_device_lost() -> None:
    """screencap-0 AND blank ui_dump AND frames stopped -> genuinely lost."""
    device = FakeProbeDevice(screenshot_exc=EmptyScreencapError("0 bytes"))

    result = probe_screen(
        device, _never_classify, frames_flowing=lambda: False, sleep=_no_sleep([])
    )

    assert result.state is ScreenState.DEVICE_LOST
    assert device.ui_dump_calls == DUMP_ATTEMPTS  # only after every retry failed


def test_screenshot_device_error_without_frames_is_device_lost() -> None:
    """A non-answering screencap (plain DeviceError) keeps the old verdict."""
    device = FakeProbeDevice(screenshot_exc=DeviceError("device offline"))

    result = probe_screen(device, _never_classify, sleep=_no_sleep([]))

    assert result.state is ScreenState.DEVICE_LOST
    assert device.ui_dump_calls == 0


def test_screenshot_device_error_with_frames_flowing_is_not_lost() -> None:
    """Screencap channel errored but packets arrive: alive -> UNKNOWN."""
    device = FakeProbeDevice(screenshot_exc=DeviceError("adb hiccup"))

    result = probe_screen(
        device, _never_classify, frames_flowing=lambda: True, sleep=_no_sleep([])
    )

    assert result.state is ScreenState.UNKNOWN
    assert result.state is not ScreenState.DEVICE_LOST


# -- (c) the auth surface can never reach a tapping path ----------------------


def test_auth_required_policy_is_fatal_immediately_no_retries() -> None:
    policy = POLICIES[AlertCode.AUTH_REQUIRED]
    assert policy.fatal_immediately is True
    assert policy.warn_retries == 0
    assert policy.backoff_s == ()


def test_confirm_incident_upgrades_auth_wall_to_fatal_auth_code() -> None:
    """confirm_incident on the auth wall -> AUTH_REQUIRED + escalate, never
    the DEVICE_LOST retry/recovery code."""
    device = FakeProbeDevice(
        screenshot_exc=EmptyScreencapError("0 bytes"), ui_dumps=[AUTH_XML]
    )

    result = confirm_incident(device, _never_classify, AlertCode.LOOP_ERROR)

    assert result.code is AlertCode.AUTH_REQUIRED
    assert result.escalate is True
    assert POLICIES[result.code].fatal_immediately is True


def test_secure_unconfirmed_never_upgrades_to_device_lost_code() -> None:
    """SECURE_UNCONFIRMED keeps the original symptom code: neither
    confirm_incident nor the supervisor may turn it into DEVICE_LOST (and by
    construction the supervisor has no tap action for it -- unknown probe
    states hit the straight-to-FATAL 'blind taps forbidden' branch)."""
    device = FakeProbeDevice(
        screenshot_exc=EmptyScreencapError("0 bytes"),
        # Exhaust the default retries instantly (all dumps fail).
    )

    with pytest.MonkeyPatch.context() as mp:
        # confirm_incident calls probe_screen with default (real) sleeps;
        # collapse the retry waits so the test stays instant.
        mp.setattr("bot2048.runner.escalation.time.sleep", lambda s: None)
        result = confirm_incident(device, _never_classify, AlertCode.LOOP_ERROR)

    assert result.probe.state is ScreenState.SECURE_UNCONFIRMED
    assert result.code is AlertCode.LOOP_ERROR  # NOT upgraded to DEVICE_LOST
    assert result.code is not AlertCode.DEVICE_LOST
    assert result.escalate is True  # still fail-closed, just truthfully labeled

    # Supervisor-side upgrade hook agrees: no DEVICE_LOST from this state.
    upgraded = Supervisor._upgrade_code(AlertCode.LOOP_ERROR, result.probe)
    assert upgraded is AlertCode.LOOP_ERROR

"""Runner-internal control-flow errors shared across the live-pipeline modules.

Extracted so the perception gate (:mod:`bot2048.runner.observation`) and the
loop (:mod:`bot2048.runner.live`) can both raise the SAME contract-violation
error without a circular import (``live`` imports ``observation``, and the gate
raises it from :meth:`ObservationGate.parse_or_escalate`).
"""

from __future__ import annotations


class LiveLoopError(RuntimeError):
    """Contract violation inside the loop (escalates as CRASH via run_session)."""

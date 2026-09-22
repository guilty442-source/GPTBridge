"""§10.64 — resource governor state signals for backend consumers.

The standalone ``scripts/resource-governor.py`` writes
``runtime/state/resource-governor.json`` every cycle with the worker
aggregate ledger, the hysteresis regulation state and the
``worker_admission_hold`` flag.  These helpers let backend tasks consult
that state cheaply (one small JSON read per call, caller-chosen
cadence).

Both helpers fail open on missing/unreadable state: a dead governor
must not permanently pause periodic work or block activation — the
signals are only honoured while the governor actively publishes them.
"""
from __future__ import annotations

import json
from pathlib import Path

_STATE_FILE = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "resource-governor.json"
)


def _state() -> dict:
    try:
        payload = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def regulation_active() -> bool:
    """True while the governor's worker-budget control law is regulating
    (§10.64 ④: pausable periodic jobs defer while this holds)."""
    regulation = _state().get("regulation")
    return isinstance(regulation, dict) and regulation.get("active") is True


def worker_admission_hold() -> bool:
    """True while the governor asks the backend to hold new worker
    starts (§10.64 ⑤ fail-closed load shedding)."""
    return _state().get("worker_admission_hold") is True


__all__ = ["regulation_active", "worker_admission_hold"]

"""Governor concurrency-budget read side (``concurrency-budget/v1``).

The C++23 resource-governor publishes a ``concurrency_budget`` section in
``main-system/runtime/state/resource-governor.json`` every cycle (A30/A116/
A593: the governor is the sole regulator; modules adapt only inside their
declared envelopes).  This module is the thin read side for workers:

* :func:`class_quota` returns the effective ``(workers, paused)`` tuple for
  a work class, or ``None`` when the governor has published nothing usable.
* Fail-open to static envelopes: missing/stale state, a disabled or
  absent budget section, or a kill-switch-disabled governor all yield
  ``None`` so the caller applies its own declared maximum — a dead
  governor must never deadlock the fleet (same contract as
  ``tasks/resource_governor_signal.py``).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

_BUDGET_CONTRACT = "concurrency-budget/v1"

# A budget older than this factor of its publish interval is stale; the
# governor's default interval is 20 s, so ~90 s of silence means "not
# running" and consumers fall back to static envelopes.
_STALE_FACTOR: float = 4.5


@dataclass(frozen=True)
class ClassQuota:
    """Effective per-class decision published by the governor."""

    quota: int
    state: str  # "normal" | "throttled" | "paused"
    generation: int
    pressure: str  # "none" | "pre" | "active"

    @property
    def paused(self) -> bool:
        return self.state == "paused" or self.quota <= 0


def _read_state(state_path: Path) -> dict:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_budget(state_path: Path) -> dict | None:
    """Return the ``concurrency_budget`` object, or None when unusable.

    Unusable = file missing/unreadable, kill-switch disabled, section
    absent, wrong contract version, or the state is stale (the governor's
    ``at`` timestamp older than ~4.5 cycles).
    """
    state = _read_state(state_path)
    if not state or state.get("disabled") is True:
        return None
    budget = state.get("concurrency_budget")
    if not isinstance(budget, dict):
        return None
    if budget.get("contract") != _BUDGET_CONTRACT:
        return None
    interval = float(state.get("interval") or 20.0)
    stamp = str(state.get("at") or "")
    if stamp:
        try:
            from datetime import datetime, timezone

            published = datetime.fromisoformat(
                stamp.replace("Z", "+00:00")
            ).timestamp()
            if time.time() - published > interval * _STALE_FACTOR:
                return None
        except ValueError:
            return None
    return budget


def class_quota(
    work_class: str, state_path: Path
) -> ClassQuota | None:
    """Effective quota for one work class; None → caller's static envelope."""
    budget = read_budget(state_path)
    if budget is None:
        return None
    classes = budget.get("classes")
    if not isinstance(classes, dict):
        return None
    entry = classes.get(work_class)
    if not isinstance(entry, dict):
        return None
    try:
        quota = int(entry.get("quota") or 0)
    except (TypeError, ValueError):
        return None
    return ClassQuota(
        quota=max(0, quota),
        state=str(entry.get("state") or "normal"),
        generation=int(budget.get("generation") or 0),
        pressure=str(budget.get("pressure") or "none"),
    )


__all__ = [
    "ClassQuota",
    "class_quota",
    "read_budget",
]

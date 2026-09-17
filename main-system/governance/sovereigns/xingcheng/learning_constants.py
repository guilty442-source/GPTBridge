"""Shared constants for learning evidence sync (A185 split).

Kept here to avoid circular imports between the main module and its mixin.
"""
from __future__ import annotations

from typing import Final

RECONCILIATION_AUDIT_RELATIVE: Final[tuple[str, ...]] = (
    "main-system",
    "runtime",
    "state",
    "learning-fault-reconciliation.jsonl",
)
NON_ACTIONABLE_REMEDY: Final[str] = "no-action-required"
# An awaiting repair request whose pending action is missing is retired as
# unreconcilable — except within this grace window, so a request created
# just before its pending action is recorded is never retired by mistake.
ORPHANED_REQUEST_GRACE_SECONDS: Final[float] = 60.0
DEFAULT_RECONCILE_INTERVAL_SECONDS: Final[float] = 300.0
RECONCILE_EVENT_POLL_SECONDS: Final[float] = 5.0
RECONCILE_INTERVAL_ENV: Final[str] = "GPTBRIDGE_LEARNING_RECONCILE_INTERVAL"


__all__ = [
    "RECONCILIATION_AUDIT_RELATIVE",
    "NON_ACTIONABLE_REMEDY",
    "ORPHANED_REQUEST_GRACE_SECONDS",
    "DEFAULT_RECONCILE_INTERVAL_SECONDS",
    "RECONCILE_EVENT_POLL_SECONDS",
    "RECONCILE_INTERVAL_ENV",
]

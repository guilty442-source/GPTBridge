"""Sovereign collaboration signal function — A188/E163.

Information-layer signal for sovereign collaboration state.  This module
provides **signal-only** output — it never dispatches tasks, overrides
domain verdicts, or bypasses permission.
"""

from __future__ import annotations

from typing import Any

from core_system.sovereign_collaboration_types import (
    TERMINAL_STATES,
    TaskEnvelope,
)


def collaboration_signal(
    envelope: TaskEnvelope,
    current_state: str,
    *,
    failure_reason: str = "",
) -> dict[str, Any]:
    """Produce an information-layer signal for collaboration state (A188/E163).

    Per A188: ``FAILURE:timeout/disconnect/stale-generation/schema-failure/
    missing-proof=>typed-fail-closed-state+bounded-retry-with-jitter+resume-
    from-last-verified-checkpoint+audit``.
    """
    return {
        "signal_type": "sovereign-collaboration",
        "authority": "signal-only",
        "basis": "A188/E163",
        "task_id": envelope.task_id,
        "correlation_id": envelope.correlation_id,
        "current_state": current_state,
        "is_terminal": current_state in TERMINAL_STATES,
        "failure_reason": failure_reason,
        "action_required": (
            "fail-closed+bounded-retry+checkpoint-resume+audit"
            if failure_reason
            else "none"
        ),
        "direct_link": False,
        "authority_merge": False,
        "domain_override": False,
        "unverified_success": False,
    }

"""Sovereign collaboration verification functions — A188/E163.

Verification of state transitions, domain verdict aggregation, and
collaboration status for sovereign collaboration.  This module provides
**read-only verification** only — it never dispatches tasks, overrides
domain verdicts, or bypasses permission.
"""

from __future__ import annotations

from typing import Any

from core_system.sovereign_collaboration_types import (
    COLLABORATION_STATES,
    TERMINAL_STATES,
    DomainVerdict,
    TaskEnvelope,
)


# ---------------------------------------------------------------------------
# State machine verification (A188: STATE-MACHINE)
# ---------------------------------------------------------------------------

def verify_state_transition(
    current_state: str,
    target_state: str,
) -> dict[str, Any]:
    """Verify a state transition is valid (A188: STATE-MACHINE).

    Per A188: ``STATE-MACHINE:received>assignment-validated>domain-checks-
    pending>permission-validated>execution-validated>dispatched>result-
    verified>completed|denied|failed|expired``.
    """
    if current_state not in COLLABORATION_STATES:
        return {
            "ok": False,
            "reason": f"invalid-current-state:{current_state}",
        }
    if target_state not in COLLABORATION_STATES:
        return {
            "ok": False,
            "reason": f"invalid-target-state:{target_state}",
        }
    if current_state in TERMINAL_STATES:
        return {
            "ok": False,
            "reason": f"terminal-state-cannot-transition:{current_state}",
        }

    # Valid forward transitions
    valid_transitions: dict[str, frozenset[str]] = {
        "received": frozenset({"assignment-validated", "denied", "failed", "expired"}),
        "assignment-validated": frozenset({"domain-checks-pending", "denied", "failed", "expired"}),
        "domain-checks-pending": frozenset({"permission-validated", "denied", "failed", "expired"}),
        "permission-validated": frozenset({"execution-validated", "denied", "failed", "expired"}),
        "execution-validated": frozenset({"dispatched", "denied", "failed", "expired"}),
        "dispatched": frozenset({"result-verified", "failed", "expired"}),
        "result-verified": frozenset({"completed", "denied", "failed", "expired"}),
    }

    allowed = valid_transitions.get(current_state, frozenset())
    if target_state not in allowed:
        return {
            "ok": False,
            "reason": f"invalid-transition:{current_state}->{target_state}",
        }

    return {
        "ok": True,
        "reason": "",
        "from": current_state,
        "to": target_state,
    }


# ---------------------------------------------------------------------------
# Domain verdict aggregation (A188: CONCURRENCY)
# ---------------------------------------------------------------------------

def aggregate_domain_verdicts(
    verdicts: list[DomainVerdict],
) -> dict[str, Any]:
    """Aggregate parallel domain verdicts (A188: CONCURRENCY).

    Per A188: ``CONCURRENCY:independent-domain-checks parallel-after-
    assignment+dependent-gates serial+result-join requires all mandatory-
    current proofs``.

    A single deny from any domain blocks the task.  A defer does not block
    but requires all non-deferred verdicts to be approve.
    """
    if not verdicts:
        return {
            "ok": False,
            "reason": "no-domain-verdicts",
            "basis": "A188/E163",
        }

    denies = [v for v in verdicts if v.is_denied]
    defers = [v for v in verdicts if v.decision == "defer"]
    approves = [v for v in verdicts if v.is_approved]

    if denies:
        return {
            "ok": False,
            "reason": "domain-denied",
            "basis": "A188/E163",
            "denied_by": [v.sovereign_id for v in denies],
            "deny_reasons": [v.reason for v in denies],
            "all_verdicts": [v.as_dict() for v in verdicts],
        }

    if defers and len(approves) + len(defers) == len(verdicts):
        return {
            "ok": False,
            "reason": "domain-deferred",
            "basis": "A188/E163",
            "deferred_by": [v.sovereign_id for v in defers],
            "all_verdicts": [v.as_dict() for v in verdicts],
        }

    return {
        "ok": True,
        "reason": "",
        "basis": "A188/E163",
        "approved_count": len(approves),
        "all_verdicts": [v.as_dict() for v in verdicts],
    }


# ---------------------------------------------------------------------------
# Collaboration status (A188: OBSERVABILITY)
# ---------------------------------------------------------------------------

def collaboration_status(
    envelope: TaskEnvelope,
    current_state: str,
    *,
    domain_verdicts: list[DomainVerdict] | None = None,
) -> dict[str, Any]:
    """Return the collaboration status for observability (A188: OBSERVABILITY).

    Per A188: ``OBSERVABILITY:one-correlation-chain+owner/participant/
    timestamps/decision/proof/state-transition/latency/retry visible-at-
    authorized-level``.
    """
    return {
        "task_id": envelope.task_id,
        "correlation_id": envelope.correlation_id,
        "assigned_owner": envelope.assigned_owner,
        "participants": list(envelope.participant_list),
        "current_state": current_state,
        "is_terminal": current_state in TERMINAL_STATES,
        "state_sequence": envelope.state_sequence,
        "runtime_generation": envelope.runtime_generation,
        "release_id": envelope.release_id,
        "domain_verdicts": (
            [v.as_dict() for v in domain_verdicts] if domain_verdicts else []
        ),
        "basis": "A188/E163",
        "entry": "information-layer://official",
        "delivery": "typed-envelope+idempotency+ordered-sequence+ack+bounded-replay",
    }

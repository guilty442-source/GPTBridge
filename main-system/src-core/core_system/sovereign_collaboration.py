"""Sovereign collaboration and information-layer handoff — A188/E163.

Per A188 (sovereign-collaboration-and-information-layer-handoff) and E163
(sovereign-collaboration-flow), all sovereign requests, events, results,
decisions, and proofs flow through the information-layer official entry.

Collaboration unit (A188: COLLABORATION-UNIT): task-envelope containing
task-id, request-id, origin, objective, scope, priority, deadline,
dependency-set, input-contract, expected-output, assigned-owner,
participant-list, runtime-generation, release-id, state-sequence,
correlation-id.

State machine (A188: STATE-MACHINE): received > assignment-validated >
domain-checks-pending > permission-validated > execution-validated >
dispatched > result-verified > completed | denied | failed | expired.

Concurrency (A188: CONCURRENCY): independent-domain-checks parallel-after-
assignment + dependent-gates serial + result-join requires all mandatory
current proofs.

Delivery (A188: DELIVERY): at-least-once-transport + idempotency-key +
deduplication + monotonic-per-task-sequence + acknowledgement.

This module provides **read-only data structures and verification**.  It
never dispatches tasks, overrides domain verdicts, or bypasses permission.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

# ---------------------------------------------------------------------------
# Collaboration state machine (A188: STATE-MACHINE)
# ---------------------------------------------------------------------------

COLLABORATION_STATES: Final[tuple[str, ...]] = (
    "received",
    "assignment-validated",
    "domain-checks-pending",
    "permission-validated",
    "execution-validated",
    "dispatched",
    "result-verified",
    "completed",
    "denied",
    "failed",
    "expired",
)

TERMINAL_STATES: Final[frozenset[str]] = frozenset({
    "completed",
    "denied",
    "failed",
    "expired",
})

# A188: FLOW — the collaboration flow stages
COLLABORATION_FLOW: Final[tuple[str, ...]] = (
    "information-entry",
    "system-decision-assignment",
    "parallel-domain-verdicts",
    "permission-validation",
    "system-runtime-validation",
    "governed-executor",
    "result-verification",
    "authorized-publication",
)


# ---------------------------------------------------------------------------
# Task envelope (A188: COLLABORATION-UNIT)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskEnvelope:
    """Typed task envelope for sovereign collaboration (A188: COLLABORATION-UNIT).

    Per A188: ``COLLABORATION-UNIT:task-envelope{task-id,request-id,origin,
    objective,scope,priority,deadline,dependency-set,input-contract,
    expected-output,assigned-owner,participant-list,runtime-generation,
    release-id,state-sequence,correlation-id}``.
    """

    task_id: str
    request_id: str
    origin: str
    objective: str
    scope: str
    priority: str
    deadline: str
    dependency_set: tuple[str, ...]
    input_contract: str
    expected_output: str
    assigned_owner: str
    participant_list: tuple[str, ...]
    runtime_generation: str
    release_id: str
    state_sequence: int
    correlation_id: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Domain verdict (A188: DOMAIN-VERDICT)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DomainVerdict:
    """Typed domain verdict from a specialized sovereign (A188: DOMAIN-VERDICT).

    Per A188: ``DOMAIN-VERDICT:each-specialized-sovereign decides-only-its-
    declared-domain and returns typed approve/deny/defer-with-reason/
    evidence/constraints/expiry``.
    """

    sovereign_id: str
    domain: str
    decision: str  # "approve" | "deny" | "defer"
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    expiry: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_approved(self) -> bool:
        return self.decision == "approve"

    @property
    def is_denied(self) -> bool:
        return self.decision == "deny"


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


__all__ = [
    "COLLABORATION_FLOW",
    "COLLABORATION_STATES",
    "DomainVerdict",
    "TERMINAL_STATES",
    "TaskEnvelope",
    "aggregate_domain_verdicts",
    "collaboration_signal",
    "collaboration_status",
    "verify_state_transition",
]

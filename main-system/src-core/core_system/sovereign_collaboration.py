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

from core_system.sovereign_collaboration_signal import collaboration_signal
from core_system.sovereign_collaboration_types import (
    COLLABORATION_FLOW,
    COLLABORATION_STATES,
    TERMINAL_STATES,
    DomainVerdict,
    TaskEnvelope,
)
from core_system.sovereign_collaboration_verify import (
    aggregate_domain_verdicts,
    collaboration_status,
    verify_state_transition,
)

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

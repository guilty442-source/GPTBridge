"""Sovereign collaboration types and constants — A188/E163.

Constants and immutable dataclasses for sovereign collaboration and
information-layer handoff.  This module provides **read-only data
structures** only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
    "decision-assignment",
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

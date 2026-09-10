"""Validation chain types and constants — A187/E162.

Constants and immutable proof dataclasses for the separated sovereign
validation chain.  This module provides **read-only data structures** only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Final

# ---------------------------------------------------------------------------
# Validation chain stages (A187/E162)
# ---------------------------------------------------------------------------

VALIDATION_CHAIN_STAGES: Final[tuple[str, ...]] = (
    "task-assignment",
    "permission",
    "execution",
    "governed-executor",
    "post-execution-verification",
)

VALIDATION_CHAIN_OWNERS: Final[dict[str, str]] = {
    "task-assignment": "system-decision-sovereign",
    "permission": "permission-sovereign",
    "execution": "system-runtime-sovereign",
    "governed-executor": "system-runtime-sovereign",
    "post-execution-verification": "system-runtime-sovereign",
}


# ---------------------------------------------------------------------------
# Proof structures (A187: PROOF-BINDING)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AssignmentProof:
    """Immutable task-assignment proof issued by system-decision-sovereign.

    Per A187: ``TASK-ASSIGNMENT-VALIDATION:owner=system-decision-sovereign+
    verify-task-identity/purpose/source/one-duty-owner/duty-capability-match/
    scope/dependency/priority/input-output-contract/non-overlap+issue-
    immutable-assignment-proof``.
    """

    task_id: str
    request_id: str
    assigned_owner: str
    duty: str
    scope: str
    priority: str
    input_contract: str
    expected_output: str
    runtime_generation: str
    release_id: str
    issuer: str
    issued_at: str
    expiry: str
    single_use: bool
    content_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_expired(self) -> bool:
        if not self.expiry:
            return False
        try:
            return datetime.now(timezone.utc) > datetime.fromisoformat(self.expiry)
        except (ValueError, TypeError):
            return False


@dataclass(frozen=True)
class PermissionProof:
    """Immutable permission proof issued by permission-sovereign.

    Per A187: ``PERMISSION-VALIDATION:owner=permission-sovereign+verify-
    subject/assignment-proof/action/target/resource-and-data-scope/purpose/
    session/expiry/grant/revocation/least-privilege+issue-allow-or-deny-
    permission-proof``.
    """

    task_id: str
    subject_identity: str
    action: str
    target: str
    resource_scope: str
    data_scope: str
    purpose: str
    decision: str  # "allow" or "deny"
    session_generation: str
    expiry: str
    issuer: str
    issued_at: str
    content_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_allowed(self) -> bool:
        return self.decision == "allow"

    @property
    def is_expired(self) -> bool:
        if not self.expiry:
            return False
        try:
            return datetime.now(timezone.utc) > datetime.fromisoformat(self.expiry)
        except (ValueError, TypeError):
            return False


@dataclass(frozen=True)
class ExecutionProof:
    """Immutable execution verification proof issued by system-runtime-sovereign.

    Per A187: ``EXECUTION-VALIDATION:owner=system-runtime-sovereign+pre-verify-
    exact-assignment-and-permission-proofs/executor-identity/active-certified-
    release/runtime-generation/dependencies/resources/input-contract/
    idempotency/timeout/checkpoint/rollback/health/readiness+dispatch-only-
    after-pass-to-governed-executor+post-verify-result-contract/side-effect-
    scope/hash/state/health/stability-window+issue-execution-verification-proof``.
    """

    task_id: str
    request_id: str
    executor_identity: str
    release_id: str
    runtime_generation: str
    pre_verified: bool
    dispatched: bool
    post_verified: bool
    result_hash: str
    side_effect_scope: str
    stability_window: str
    issuer: str
    issued_at: str
    content_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Chain verification result (A187: ORDER + SEPARATION)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChainValidationResult:
    """Result of verifying the complete validation chain (A187/E162)."""

    ok: bool
    completed_stages: tuple[str, ...]
    failures: tuple[str, ...]
    assignment_proof: AssignmentProof | None = None
    permission_proof: PermissionProof | None = None
    execution_proof: ExecutionProof | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "completed_stages": list(self.completed_stages),
            "failures": list(self.failures),
            "assignment_proof": self.assignment_proof.as_dict() if self.assignment_proof else None,
            "permission_proof": self.permission_proof.as_dict() if self.permission_proof else None,
            "execution_proof": self.execution_proof.as_dict() if self.execution_proof else None,
        }

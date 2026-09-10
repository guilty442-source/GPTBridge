"""Separated sovereign validation chain — A187/E162.

Per A187 (separated-sovereign-validation-chain) and E162 (sovereign-
validation-separation), three validation gates are strictly separated and
non-skippable, each owned by a different sovereign:

  1. **Task-assignment validation** — owner: system-decision-sovereign
     Verifies task identity, purpose, source, one-duty-owner, duty-capability
     match, scope, dependency, priority, input-output-contract, non-overlap.
     Issues immutable assignment-proof.

  2. **Permission validation** — owner: permission-sovereign
     Verifies subject, assignment-proof, action, target, resource-and-data-
     scope, purpose, session, expiry, grant, revocation, least-privilege.
     Issues allow-or-deny permission-proof.

  3. **Execution validation** — owner: system-runtime-sovereign
     Pre-verifies exact assignment and permission proofs, executor identity,
     active-certified-release, runtime-generation, dependencies, resources,
     input-contract, idempotency, timeout, checkpoint, rollback, health,
     readiness.  Dispatches to governed executor.  Post-verifies result-
     contract, side-effect-scope, hash, state, health, stability-window.
     Issues execution-verification-proof.

Order (A187: ORDER): strict and non-skippable.
Separation (A187: SEPARATION): assignment-proof does not grant permission or
authorize execution; permission-proof does not assign duty or prove runtime
readiness; execution-proof does not create permission or change assignment.

This module provides **read-only data structures and verification**.  It
never grants permission, assigns tasks, or dispatches execution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
# Chain verification (A187: ORDER + SEPARATION)
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


def _check_assignment_stage(
    proof: AssignmentProof | None,
) -> tuple[list[str], list[str]]:
    """Check task-assignment stage (A187: ORDER stage 1)."""
    completed: list[str] = []
    failures: list[str] = []
    if proof is None:
        failures.append("missing-assignment-proof")
    elif proof.is_expired:
        failures.append("expired-assignment-proof")
    else:
        completed.append("task-assignment")
    return completed, failures


def _check_permission_stage(
    proof: PermissionProof | None,
    assignment: AssignmentProof | None,
    assignment_done: bool,
) -> tuple[list[str], list[str]]:
    """Check permission stage (A187: ORDER stage 2)."""
    completed: list[str] = []
    failures: list[str] = []
    if not assignment_done:
        if assignment is not None:
            failures.append("permission-stage-skipped")
        return completed, failures
    if proof is None:
        failures.append("missing-permission-proof")
    elif proof.is_expired:
        failures.append("expired-permission-proof")
    elif not proof.is_allowed:
        failures.append("permission-denied")
    elif proof.task_id != (assignment.task_id if assignment else ""):
        failures.append("permission-proof-task-id-mismatch")
    else:
        completed.append("permission")
    return completed, failures


def _check_execution_stage(
    proof: ExecutionProof | None,
    assignment: AssignmentProof | None,
    permission_done: bool,
    permission_allowed: bool,
) -> tuple[list[str], list[str]]:
    """Check execution stage (A187: ORDER stage 3)."""
    completed: list[str] = []
    failures: list[str] = []
    if not permission_done:
        if permission_allowed:
            failures.append("execution-stage-skipped")
        return completed, failures
    if proof is None:
        failures.append("missing-execution-proof")
    elif not proof.pre_verified:
        failures.append("execution-pre-verification-failed")
    elif not proof.dispatched:
        failures.append("execution-not-dispatched")
    elif not proof.post_verified:
        failures.append("execution-post-verification-failed")
    elif proof.task_id != (assignment.task_id if assignment else ""):
        failures.append("execution-proof-task-id-mismatch")
    else:
        completed.append("execution")
    return completed, failures


def verify_validation_chain(
    *,
    assignment_proof: AssignmentProof | None = None,
    permission_proof: PermissionProof | None = None,
    execution_proof: ExecutionProof | None = None,
) -> ChainValidationResult:
    """Verify the complete validation chain (A187: ORDER + SEPARATION)."""
    completed: list[str] = []
    failures: list[str] = []

    c, f = _check_assignment_stage(assignment_proof)
    completed.extend(c); failures.extend(f)

    c, f = _check_permission_stage(
        permission_proof, assignment_proof, "task-assignment" in completed,
    )
    completed.extend(c); failures.extend(f)

    c, f = _check_execution_stage(
        execution_proof, assignment_proof,
        "permission" in completed,
        permission_proof is not None and permission_proof.is_allowed,
    )
    completed.extend(c); failures.extend(f)

    return ChainValidationResult(
        ok=len(failures) == 0 and len(completed) == 3,
        completed_stages=tuple(completed),
        failures=tuple(failures),
        assignment_proof=assignment_proof,
        permission_proof=permission_proof,
        execution_proof=execution_proof,
    )


def validation_chain_signal(
    result: ChainValidationResult,
) -> dict[str, Any]:
    """Produce an information-layer signal for validation chain status (A187/E162).

    Per A187: ``FAILURE:any-gate=>stop+fail-closed+typed-result+audit``.
    """
    return {
        "signal_type": "validation-chain",
        "authority": "signal-only",
        "basis": "A187/E162",
        "ok": result.ok,
        "completed_stages": list(result.completed_stages),
        "failures": list(result.failures),
        "chain_order": list(VALIDATION_CHAIN_STAGES),
        "chain_owners": dict(VALIDATION_CHAIN_OWNERS),
        "separation": "assignment-proof!=permission-proof!=execution-proof",
        "action_required": "fail-closed+typed-result+audit" if not result.ok else "none",
        "gate_skip": False,
        "gate_reorder": False,
        "self_authorize": False,
    }


__all__ = [
    "AssignmentProof",
    "ChainValidationResult",
    "ExecutionProof",
    "PermissionProof",
    "VALIDATION_CHAIN_OWNERS",
    "VALIDATION_CHAIN_STAGES",
    "validation_chain_signal",
    "verify_validation_chain",
]

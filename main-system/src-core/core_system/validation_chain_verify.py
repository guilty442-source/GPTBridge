"""Validation chain verification functions — A187/E162.

Verification of the separated sovereign validation chain.  This module
provides **read-only verification** only — it never grants permission,
assigns tasks, or dispatches execution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core_system.validation_chain_types import (
    AssignmentProof,
    ChainValidationResult,
    ExecutionProof,
    PermissionProof,
)

if TYPE_CHECKING:
    pass


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

"""Separated sovereign validation chain — A187/E162.

Per A187 (separated-sovereign-validation-chain) and E162 (sovereign-
validation-separation), three validation gates are strictly separated and
non-skippable, each owned by a different sovereign:

  1. **Task-assignment validation** — owner: decision-sovereign
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

from core_system.validation_chain_signal import validation_chain_signal
from core_system.validation_chain_types import (
    VALIDATION_CHAIN_OWNERS,
    VALIDATION_CHAIN_STAGES,
    AssignmentProof,
    ChainValidationResult,
    ExecutionProof,
    PermissionProof,
)
from core_system.validation_chain_verify import verify_validation_chain

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

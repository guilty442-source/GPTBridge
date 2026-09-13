"""Governance-compliant automatic repair system — facade.

This module is a re-export facade; the implementation lives in submodules:

  * :mod:`core_system.auto_repair_chain_types` — types, enums, dataclasses, audit.
  * :mod:`core_system.auto_repair_chain_health` — MaintenanceHealthClassifier.
  * :mod:`core_system.auto_repair_chain_decision` — RepairObjectiveAssigner.
  * :mod:`core_system.auto_repair_chain_permission` — RepairPermissionValidator.
  * :mod:`core_system.auto_repair_chain_executor` — GovernedExecutor.
  * :mod:`core_system.auto_repair_chain_verify` — IndependentVerifier.
  * :mod:`core_system.auto_repair_chain_learning` — RepairLearningStore.

The :class:`AutoRepairOrchestrator` and the factory function
:func:`create_auto_repair_orchestrator` remain here as the chain
entry point.

Key governance rules enforced:
- A258: No overwrite/reset preservation boundary
- A259: Exact root-cause evidence > preserve > exact decision/permission/programming-review proof > smallest deterministic targeted patch > post-hash/diff/tests/audit/stability
- A261: Smallest targeted patch plan
- Learning-system-sovereign: verified-repeatable-recipes-only via maintenance-governed-executor
- No component-self-authorized-repair, no repair-without-decision-proof, no success-before-verification
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from governance_rule.execution.authentication import GovernanceAuthenticationService

from core_system.auto_repair_chain_decision import RepairObjectiveAssigner
from core_system.auto_repair_chain_executor import GovernedExecutor
from core_system.auto_repair_chain_health import MaintenanceHealthClassifier
from core_system.auto_repair_chain_learning import RepairLearningStore
from core_system.auto_repair_chain_permission import RepairPermissionValidator
from core_system.auto_repair_chain_types import (
    GovernanceAudit,
    HealthSignal,
    HealthState,
    LearnedRecipe,
    PermissionGrant,
    RepairDecision,
    RepairExecution,
    RepairObjective,
    RepairPlan,
    VerificationResult,
)
from core_system.auto_repair_chain_verify import IndependentVerifier


class AutoRepairOrchestrator:
    """Orchestrates the full governance-compliant repair chain.

    CHAIN: SIGNAL > information-layer > maintenance-health-classification >
           decision-repair-decision > permission-validation >
           system-runtime-or-system-programming-dispatch > governed-executor >
           independent-verification > information-layer > ui
    """

    def __init__(
        self,
        project_root: Path,
        auth_service: GovernanceAuthenticationService,
    ):
        self.project_root = project_root
        self.repair_root = project_root / "main-system" / "data" / "automatic-repair"
        self.repair_root.mkdir(parents=True, exist_ok=True)

        self.audit = GovernanceAudit(self.repair_root / "audit")
        self.health_classifier = MaintenanceHealthClassifier(project_root, self.audit)
        self.decision_sovereign = RepairObjectiveAssigner(project_root, self.audit)
        self.permission_sovereign = RepairPermissionValidator(project_root, auth_service, self.audit)
        self.executor = GovernedExecutor(project_root, self.audit)
        self.verifier = IndependentVerifier(project_root, self.audit)
        self.learning_store = RepairLearningStore(self.repair_root, self.audit)

    def process_health_signal(
        self,
        signal: HealthSignal,
        actor: str = "information-layer",
        *,
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Process a health signal through the full repair chain."""
        # Stage 1: Health Classification (health-maintenance-test-sub-sovereign)
        classification = self.health_classifier.classify([signal])

        # If healthy, no further action
        if classification["overall_state"] == HealthState.HEALTHY:
            return {"stage": "health_classification", "result": "healthy", "classification": classification}

        # User-confirmation gate: while automatic repair execution is
        # disabled the chain records the signal but performs no decision,
        # no permission grant, and no mutation.  The request waits for the
        # user to confirm this fault in the assistant panel.  An explicit
        # per-item confirmation (user_confirmed=True) bypasses the gate.
        from .auto_action_policy import automatic_repair_execution_allowed

        if not automatic_repair_execution_allowed() and not user_confirmed:
            return {
                "stage": "awaiting-user-confirmation",
                "result": "deferred",
                "classification": classification,
                "reason": (
                    "automatic repair execution is disabled; "
                    "confirm this fault in the assistant panel"
                ),
            }

        # Stage 2: Repair Decision (decision-sovereign)
        objective = self.decision_sovereign.assign_repair_objective(
            classification,
            signal.evidence.get("fault_code", "UNKNOWN"),
            signal.evidence,
        )

        if objective is None:
            return {"stage": "repair_decision", "result": "no_repair_needed", "classification": classification}

        # Stage 3: Permission Validation (permission-sovereign)
        grant = self.permission_sovereign.validate_and_grant(objective, actor)

        if grant is None:
            return {"stage": "permission_validation", "result": "denied", "objective": asdict(objective)}

        # Stage 4: Create Repair Plan
        plan = self._create_repair_plan(objective, grant)

        # Stage 5: Dispatch to Governed Executor
        execution = self.executor.execute(plan, grant)

        # Stage 6: Independent Verification
        verification_result, verification_evidence = self.verifier.verify(execution, plan, grant)

        # Update execution with verification
        execution = RepairExecution(
            execution_id=execution.execution_id,
            plan_id=execution.plan_id,
            executor_id=execution.executor_id,
            started_at=execution.started_at,
            completed_at=execution.completed_at,
            steps_completed=execution.steps_completed,
            postimage_hashes=execution.postimage_hashes,
            diff=execution.diff,
            verification=verification_result,
            verification_evidence=verification_evidence,
            rollback_triggered=execution.rollback_triggered,
            error=execution.error,
        )

        # Stage 7: Learn if verified
        if verification_result == VerificationResult.PASSED:
            self._learn_from_success(objective, plan, execution, grant)

        # Stage 8: Report back through information layer
        return {
            "stage": "complete",
            "objective": asdict(objective),
            "grant": asdict(grant),
            "plan": asdict(plan),
            "execution": asdict(execution),
            "verification": verification_result.value,
            "verification_evidence": verification_evidence,
        }

    def _create_repair_plan(self, objective: RepairObjective, grant: PermissionGrant) -> RepairPlan:
        """Create minimal targeted repair plan per A261."""
        # Compute preimage hashes
        pre_hashes = {}
        for path in grant.path_scope:
            full_path = self.project_root / path
            if full_path.exists():
                if full_path.is_file():
                    pre_hashes[path] = hashlib.sha256(full_path.read_bytes()).hexdigest()
                elif full_path.is_dir():
                    for file in full_path.rglob("*"):
                        if file.is_file():
                            rel = file.relative_to(self.project_root).as_posix()
                            pre_hashes[rel] = hashlib.sha256(file.read_bytes()).hexdigest()

        # Build steps based on objective
        steps = []
        if objective.fault_code in ("MAIN_SYSTEM_SOURCE_SYNTAX_FAILED", "IndentationError", "TabError", "SyntaxError"):
            file_path = objective.root_cause_evidence.get("file", "")
            if file_path:
                steps.append({
                    "action": "targeted_patch",
                    "target": file_path,
                    "precondition": "file_has_syntax_error",
                })
        elif objective.fault_code in ("EXECUTABLE_MISSING", "PACKAGE_UNVERIFIED", "INCOMPATIBLE_TOOL_RUNTIME"):
            tool_id = objective.root_cause_evidence.get("tool_id", "")
            if tool_id:
                steps.append({
                    "action": "rebuild_artifact",
                    "target": tool_id,
                    "precondition": "owned_databases_intact",
                })

        return RepairPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:12]}",
            objective_id=objective.objective_id,
            method="targeted_patch" if objective.scope.get("action") == "patch" else "artifact_rebuild",
            steps=steps,
            preimage_hashes=pre_hashes,
            verification_criteria=["compile-ok", "tests-pass", "governance-audit", "stability"],
            rollback_plan={"action": "restore_from_preimage", "hashes": pre_hashes},
            estimated_duration_seconds=60,
        )

    def _learn_from_success(
        self,
        objective: RepairObjective,
        plan: RepairPlan,
        execution: RepairExecution,
        grant: PermissionGrant,
    ) -> None:
        """Learn from successful verified repair."""
        signature_hash = hashlib.sha256(
            f"{objective.fault_code}:{objective.root_cause_evidence.get('file', '')}:{plan.method}".encode()
        ).hexdigest()[:16]

        self.learning_store.record_outcome(
            signature_hash=signature_hash,
            error_class=objective.fault_code,
            message_pattern=str(objective.root_cause_evidence)[:200],
            failure_code=objective.fault_code,
            remedy=plan.method,
            ok=True,
            verification_result=execution.verification,
            detail={
                "objective_id": objective.objective_id,
                "plan_id": plan.plan_id,
                "execution_id": execution.execution_id,
                "diff": execution.diff,
            },
        )

        # Check for promotion
        analysis = self.learning_store.analyze_history()
        for candidate in analysis.get("candidates", []):
            if candidate["signature_hash"] == signature_hash:
                self.learning_store.promote_recipe(candidate)
                break


def create_auto_repair_orchestrator(
    project_root: Path,
    auth_service: GovernanceAuthenticationService,
) -> AutoRepairOrchestrator:
    """Factory function to create the governance-compliant auto-repair orchestrator."""
    return AutoRepairOrchestrator(project_root, auth_service)


__all__ = [
    "HealthState",
    "RepairDecision",
    "VerificationResult",
    "HealthSignal",
    "RepairObjective",
    "PermissionGrant",
    "RepairPlan",
    "RepairExecution",
    "LearnedRecipe",
    "MaintenanceHealthClassifier",
    "RepairObjectiveAssigner",
    "RepairPermissionValidator",
    "GovernedExecutor",
    "IndependentVerifier",
    "RepairLearningStore",
    "AutoRepairOrchestrator",
    "create_auto_repair_orchestrator",
]

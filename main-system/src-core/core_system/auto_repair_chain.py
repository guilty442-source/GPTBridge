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
- A261: No overwrite/reset preservation boundary
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
from core_system.maintenance_retry_policy import MaintenanceRetryPolicy
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
from core_system.auto_repair_chain_util import iter_scope_files
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
        # G49/§10.4: bounded retry + cooldown — a fault may not re-trigger
        # repairs indefinitely; budget exhaustion escalates instead.
        self.retry_policy = MaintenanceRetryPolicy(
            project_root / "main-system" / "runtime" / "state"
            / "maintenance-retry-policy.json"
        )

    def process_health_signal(
        self,
        signal: HealthSignal,
        actor: str = "information-layer",
        *,
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Process a health signal through the full repair chain."""
        # Stage 1: Health Classification (decision-core maintenance module)
        classification = self.health_classifier.classify([signal])

        # If healthy, no further action
        if classification["overall_state"] == HealthState.HEALTHY:
            return {"stage": "health_classification", "result": "healthy", "classification": classification}

        # Stage 2: Repair Decision (decision-sovereign)
        objective = self.decision_sovereign.assign_repair_objective(
            classification,
            signal.evidence.get("fault_code", "UNKNOWN"),
            signal.evidence,
        )

        if objective is None:
            return {"stage": "repair_decision", "result": "no_repair_needed", "classification": classification}

        # Stage 2.5 (§10.4): bounded retry + cooldown gate — prevents a fault
        # from re-triggering repairs faster than the budget allows.
        fault_code = str(signal.evidence.get("fault_code", "UNKNOWN"))
        decision = self.retry_policy.check(fault_code)
        if not decision.allowed:
            return {
                "stage": "retry_policy",
                "result": "blocked",
                "reason": decision.reason,
                "retry_after_s": decision.retry_after_s,
                "escalated": decision.escalated,
                "objective": asdict(objective),
            }
        self.retry_policy.record_attempt(
            fault_code,
            pre_state={"overall_state": classification["overall_state"].value},
        )

        # Stage 3: Permission Validation (permission-sovereign)
        grant = self.permission_sovereign.validate_and_grant(objective, actor)

        if grant is None:
            return {"stage": "permission_validation", "result": "denied", "objective": asdict(objective)}

        # Stage 4: Create Repair Plan
        plan = self._create_repair_plan(objective, grant)

        # Governor directive (2026-09-18): the per-item user-confirmation
        # gate is retired — both tiers (``targeted_patch`` mutation and
        # ``artifact_rebuild`` stability recovery) execute through this
        # chain under the system-audit flow.  The plan remains read-only
        # evidence recorded ahead of execution.

        return self._execute_verify_report(objective, grant, plan)

    def _execute_verify_report(
        self, objective: Any, grant: Any, plan: Any
    ) -> dict[str, Any]:
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

        # Stage 7: Finalize — verification verdict decides whether the
        # retained backups are discarded (passed) or restored (failed),
        # so an unverified change never stays on disk.
        passed = verification_result == VerificationResult.PASSED
        # §10.4: post-repair verification feeds the retry budget —
        # unverified/failed repairs consume attempts toward cooldown.
        self.retry_policy.record_outcome(
            str(objective.fault_code), verified=passed
        )
        finalization = self.executor.finalize(plan, passed)

        # Stage 8: Learn — both verdicts feed the store so the recorded
        # success rate is honest (a failed repair must not look like a
        # repeatable success).
        self._record_outcome(objective, plan, execution, grant, ok=passed)

        # Stage 9: Report back through information layer
        return {
            "stage": "complete",
            "objective": asdict(objective),
            "grant": asdict(grant),
            "plan": asdict(plan),
            "execution": asdict(execution),
            "verification": verification_result.value,
            "verification_evidence": verification_evidence,
            "finalization": finalization,
        }

    def _create_repair_plan(self, objective: RepairObjective, grant: PermissionGrant) -> RepairPlan:
        """Create minimal targeted repair plan per A261."""
        # Compute preimage hashes — bounded walk (skips vendored/cache
        # trees and oversized files so a directory scope cannot dominate
        # repair latency).
        pre_hashes = {}
        for path in grant.path_scope:
            for rel, file in iter_scope_files(self.project_root, path):
                try:
                    pre_hashes[rel] = hashlib.sha256(file.read_bytes()).hexdigest()
                except OSError:
                    continue

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
        elif objective.fault_code in (
            "EXECUTABLE_MISSING", "PACKAGE_UNVERIFIED",
            "INCOMPATIBLE_TOOL_RUNTIME", "STALE_TOOL_PACKAGE",
            "SOURCE_RUNTIME_NOT_READY", "TOOL_RUNTIME_CRASH",
        ):
            tool_id = objective.root_cause_evidence.get("tool_id", "")
            if tool_id:
                steps.append({
                    "action": "rebuild_artifact",
                    "target": tool_id,
                    "precondition": "owned_databases_intact",
                })

        method = self._select_plan_method(objective)
        if not steps and method == "artifact_rebuild":
            # Learned recipe selected a rebuild for a fault code with no
            # static step mapping — apply the learned remedy's step.
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
            method=method,
            steps=steps,
            preimage_hashes=pre_hashes,
            verification_criteria=["compile-ok", "tests-pass", "governance-audit", "stability"],
            rollback_plan={"action": "restore_from_preimage", "hashes": pre_hashes},
            estimated_duration_seconds=60,
        )

    # Runtime-safe plan methods a learned recipe may select.  Learned
    # knowledge stays advisory and bounded to non-mutating stability
    # recovery; ``targeted_patch`` (source mutation) is deliberately
    # absent so a learned recipe can never self-authorize a patch.
    _LEARNABLE_METHODS = frozenset({"artifact_rebuild"})

    def _select_plan_method(self, objective: RepairObjective) -> str:
        """Select the repair method; learned recipes may override.

        The default method follows the objective scope.  When the
        learning store holds a promoted, verified-repeatable recipe for
        this fault code whose remedy is a runtime-safe method, the
        learned method wins — the learning loop closes here.  Mutation
        methods are never learned-selectable.
        """
        default = (
            "targeted_patch"
            if objective.scope.get("action") == "patch"
            else "artifact_rebuild"
        )
        learned = self._learned_method_for(objective)
        return learned or default

    def _learned_method_for(self, objective: RepairObjective) -> str | None:
        """Return the best promoted runtime-safe remedy for this fault code."""
        try:
            recipes = self.learning_store.get_learned_recipes()
        except Exception:
            return None
        best_rate = 0.0
        best_remedy: str | None = None
        for recipe in recipes:
            if str(recipe.get("error_class") or "") != objective.fault_code:
                continue
            remedy = str(recipe.get("remedy") or "")
            if remedy not in self._LEARNABLE_METHODS:
                continue
            rate = float(recipe.get("success_rate") or 0.0)
            count = int(recipe.get("occurrence_count") or 0)
            if rate >= 0.8 and count >= 3 and rate > best_rate:
                best_rate = rate
                best_remedy = remedy
        return best_remedy

    def _record_outcome(
        self,
        objective: RepairObjective,
        plan: RepairPlan,
        execution: RepairExecution,
        grant: PermissionGrant,
        *,
        ok: bool,
    ) -> None:
        """Record the verified outcome; promote repeatable successes."""
        signature_hash = hashlib.sha256(
            f"{objective.fault_code}:{objective.root_cause_evidence.get('file', '')}:{plan.method}".encode()
        ).hexdigest()[:16]

        self.learning_store.record_outcome(
            signature_hash=signature_hash,
            error_class=objective.fault_code,
            message_pattern=str(objective.root_cause_evidence)[:200],
            failure_code=objective.fault_code,
            remedy=plan.method,
            ok=ok,
            verification_result=execution.verification,
            detail={
                "objective_id": objective.objective_id,
                "plan_id": plan.plan_id,
                "execution_id": execution.execution_id,
                "diff": execution.diff,
            },
        )

        if not ok:
            return
        # Promotion check is scoped to this signature — no need to
        # re-analyze unrelated history.
        for candidate in self.learning_store.analyze_history(signature_hash).get(
            "candidates", []
        ):
            self.learning_store.promote_recipe(candidate)


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

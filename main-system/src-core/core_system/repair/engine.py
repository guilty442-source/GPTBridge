"""Repair Engine — Core Engine (Phase 3).

Integrates Diagnosis Engine + Remediation Registry.
Precondition → Dry-run → Execute → Post-check → Audit.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from .diagnosis import DiagnosisEngine, DiagnosisCode, DiagnosisResult, Severity
from .remediation import (
    RemediationRegistry,
    RemediationAction,
    RiskClass,
    RemediationStatus,
    DryRunResult,
    DEFAULT_REMEDIATION_REGISTRY,
)

_logger = logging.getLogger("gptbridge.repair.engine")


class IncidentState(str, Enum):
    """Incident lifecycle states."""
    DETECTED = "DETECTED"
    DIAGNOSED = "DIAGNOSED"
    PLANNED = "PLANNED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    ACTION_COMPLETED_BUT_NOT_RECOVERED = "ACTION_COMPLETED_BUT_NOT_RECOVERED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    ESCALATED = "ESCALATED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class Incident:
    """Incident record with full lifecycle tracking."""
    incident_id: str
    diagnosis: "DiagnosisResult"
    state: IncidentState
    created_at: str
    updated_at: str
    remediation_id: Optional[str] = None
    remediation_version: Optional[str] = None
    remediation_status: Optional[RemediationStatus] = None
    scope_fence: dict[str, Any] = field(default_factory=dict)
    generation_fence: Optional[int] = None
    dry_run_result: Optional[Any] = None
    execution_result: Optional[dict[str, Any]] = None
    verification_result: Optional[dict[str, Any]] = None
    audit_record: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class GenerationFence:
    """Generation fence for remediation safety."""

    def __init__(self, get_current_generation: Callable[[], int]) -> None:
        self._get_generation = get_current_generation

    def check(self, expected_generation: int) -> tuple[bool, str]:
        """Verify generation hasn't changed."""
        current = self._get_generation()
        if current != expected_generation:
            return False, f"Generation changed: expected {expected_generation}, got {current}"
        return True, "Generation fence passed"


class ScopeFence:
    """Scope fence for remediation isolation."""

    def __init__(self, get_current_context: Callable[[], dict[str, Any]]) -> None:
        self._get_context = get_current_context

    def check(self, scope_fence: dict[str, Any]) -> tuple[bool, str]:
        """Verify remediation is within allowed scope."""
        context = self._get_context()

        # Check database scope
        if "database" in scope_fence:
            if context.get("database") != scope_fence["database"]:
                return False, f"Database scope mismatch: {context.get('database')} != {scope_fence['database']}"

        # Check module scope
        if "module" in scope_fence:
            if context.get("module") != scope_fence["module"]:
                return False, f"Module scope mismatch: {context.get('module')} != {scope_fence['module']}"

        # Check resource scope
        if "resource" in scope_fence:
            if context.get("resource") != scope_fence["resource"]:
                return False, f"Resource scope mismatch: {context.get('resource')} != {scope_fence['resource']}"

        # Check generation scope
        if "generation" in scope_fence:
            if context.get("generation") != scope_fence["generation"]:
                return False, f"Generation scope mismatch: {context.get('generation')} != {scope_fence['generation']}"

        return True, "Scope fence passed"


class LockAwareChecker:
    """Lock-aware remediation safety check."""

    def __init__(
        self,
        get_transport_backlog: Callable[[], int],
        get_active_locks: Callable[[], list[dict]],
        get_long_transactions: Callable[[], list[dict]],
        is_maintenance_window: Callable[[], bool],
    ) -> None:
        self._get_transport_backlog = get_transport_backlog
        self._get_active_locks = get_active_locks
        self._get_long_transactions = get_long_transactions
        self._is_maintenance_window = is_maintenance_window

    def check(self, remediation: "RemediationAction", context: dict[str, Any]) -> tuple[bool, str]:
        """Check if remediation is safe to execute given current locks/load."""
        # Check if maintenance window required
        if remediation.requires_maintenance_window and not self._is_maintenance_window():
            return False, "Maintenance window required but not active"

        # Check transport backlog
        backlog = self._get_transport_backlog()
        if backlog > 1000:
            return False, f"Transport backlog too high: {backlog}"

        # Check active locks
        locks = self._get_active_locks()
        if len(locks) > 10:
            return False, f"Too many active locks: {len(locks)}"

        # Check long transactions
        long_tx = self._get_long_transactions()
        if len(long_tx) > 5:
            return False, f"Too many long transactions: {len(long_tx)}"

        # Check conflicts with other running remediations
        running_remediations = context.get("running_remediations", set())
        for conflict in remediation.conflicts_with:
            if conflict in running_remediations:
                return False, f"Conflicts with running remediation: {conflict}"

        return True, "Lock-aware check passed"


class RepairEngine:
    """Core repair engine integrating diagnosis, remediation, and safety checks."""

    def __init__(
        self,
        diagnosis_engine: DiagnosisEngine,
        remediation_registry: RemediationRegistry,
        generation_fence: GenerationFence,
        scope_fence: ScopeFence,
        lock_checker: LockAwareChecker,
        get_current_context: Callable[[], dict[str, Any]],
        audit_logger: Callable[[dict[str, Any]], None],
    ) -> None:
        self.diagnosis_engine = diagnosis_engine
        self.remediation_registry = remediation_registry
        self.generation_fence = generation_fence
        self.scope_fence = scope_fence
        self.lock_checker = lock_checker
        self._get_context = get_current_context
        self._audit_logger = audit_logger

        self._incidents: dict[str, Incident] = {}
        self._running_remediations: set[str] = set()

    def process_telemetry(self, telemetry: dict[str, Any]) -> list[Incident]:
        """Main entry point: process telemetry through full repair pipeline."""
        # Phase 1: Diagnose
        diagnoses = self.diagnosis_engine.diagnose(telemetry)
        if not diagnoses:
            return []

        incidents = []
        for diagnosis in diagnoses:
            incident = self._create_incident(diagnosis)
            incidents.append(incident)

            # Phase 2: Plan remediation
            self._plan_remediation(incident)

            # Phase 3: Execute if authorized
            if incident.remediation_status == RemediationStatus.AUTHORIZED:
                self._execute_remediation(incident)

        return incidents

    def _create_incident(self, diagnosis: DiagnosisResult) -> Incident:
        """Create incident from diagnosis."""
        incident_id = f"inc-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()

        incident = Incident(
            incident_id=incident_id,
            diagnosis=diagnosis,
            state=IncidentState.DIAGNOSED,
            created_at=now,
            updated_at=now,
        )
        self._incidents[incident_id] = incident
        _logger.info("RepairEngine: created incident %s for %s", incident_id, diagnosis.diagnosis_code.value)
        return incident

    def _plan_remediation(self, incident: Incident) -> None:
        """Select and validate remediation for incident."""
        diagnosis = incident.diagnosis

        # Get available remediations
        remediations = self.remediation_registry.get(diagnosis.diagnosis_code)
        if not remediations:
            incident.state = IncidentState.QUARANTINED
            incident.error = f"No remediation registered for {diagnosis.diagnosis_code.value}"
            _logger.error("RepairEngine: no remediation for %s", diagnosis.diagnosis_code.value)
            return

        # Select best remediation (prefer lowest risk class that has preconditions met)
        context = self._get_context()
        selected = self._select_remediation(remediations, context)
        if not selected:
            incident.state = IncidentState.QUARANTINED
            incident.error = "No suitable remediation found (preconditions not met)"
            return

        # Check scope fence
        scope_ok, scope_msg = self.scope_fence.check(selected.scope_fence)
        if not scope_ok:
            incident.state = IncidentState.QUARANTINED
            incident.error = f"Scope fence failed: {scope_msg}"
            return

        # Check generation fence
        if selected.scope_fence.get("generation"):
            gen_ok, gen_msg = self.generation_fence.check(selected.scope_fence["generation"])
            if not gen_ok:
                incident.state = IncidentState.QUARANTINED
                incident.error = f"Generation fence failed: {gen_msg}"
                return

        # Check lock-aware safety
        lock_ok, lock_msg = self.lock_checker.check(selected, context)
        if not lock_ok:
            incident.state = IncidentState.QUARANTINED
            incident.error = f"Lock-aware check failed: {lock_msg}"
            return

        # Precondition check
        pre_ok, pre_msg = self._check_preconditions(selected, context)
        if not pre_ok:
            incident.state = IncidentState.QUARANTINED
            incident.error = f"Precondition failed: {pre_msg}"
            return

        # Dry-run
        dry_run_result = selected.dry_run(context)
        incident.dry_run_result = dry_run_result
        incident.remediation_id = selected.remediation_id
        incident.remediation_version = selected.version
        incident.scope_fence = selected.scope_fence
        incident.generation_fence = selected.scope_fence.get("generation")

        # Determine if authorization needed
        if selected.risk_class in (RiskClass.R2_GOVERNED_AUTO, RiskClass.R3_MANUAL_APPROVAL):
            # For now, auto-authorize R2 if policy allows
            if selected.risk_class == RiskClass.R2_GOVERNED_AUTO:
                incident.remediation_status = RemediationStatus.AUTHORIZED
            else:
                incident.remediation_status = RemediationStatus.PENDING
                incident.state = IncidentState.PLANNED
                return
        else:
            incident.remediation_status = RemediationStatus.AUTHORIZED

        incident.state = IncidentState.AUTHORIZED
        _logger.info("RepairEngine: incident %s authorized for %s", incident.incident_id, selected.remediation_id)

    def _select_remediation(
        self,
        remediations: list[RemediationAction],
        context: dict[str, Any],
    ) -> Optional[RemediationAction]:
        """Select best remediation based on risk class and preconditions."""
        # Sort by risk class (R0 < R1 < R2 < R3) then by version
        risk_order = {RiskClass.R0_OBSERVE: 0, RiskClass.R1_SAFE_AUTO: 1, RiskClass.R2_GOVERNED_AUTO: 2, RiskClass.R3_MANUAL_APPROVAL: 3}
        sorted_rems = sorted(remediations, key=lambda r: (risk_order[r.risk_class], -float(r.version)))

        for rem in sorted_rems:
            # Check preconditions
            pre_ok, _ = self._check_preconditions(rem, context)
            if pre_ok:
                return rem
        return None

    def _check_preconditions(self, action: RemediationAction, context: dict[str, Any]) -> tuple[bool, str]:
        """Check all preconditions for an action."""
        for pre in action.preconditions:
            passed, msg = pre.check(context)
            if not passed:
                return False, f"{pre.name}: {msg}"
        return True, "All preconditions passed"

    def _execute_remediation(self, incident: Incident) -> None:
        """Execute the selected remediation."""
        if not incident.remediation_id:
            return

        action = self.remediation_registry.get_by_id(incident.remediation_id, incident.remediation_version)
        if not action:
            incident.remediation_status = RemediationStatus.FAILED
            incident.error = "Remediation not found"
            return

        incident.state = IncidentState.EXECUTING
        incident.remediation_status = RemediationStatus.EXECUTING
        self._running_remediations.add(incident.remediation_id)

        start_time = time.time()
        try:
            # Re-verify generation fence before execution
            if incident.generation_fence:
                gen_ok, gen_msg = self.generation_fence.check(incident.generation_fence)
                if not gen_ok:
                    raise RuntimeError(f"Generation fence failed at execution: {gen_msg}")

            # Execute
            result = action.execute(self._get_context())
            incident.execution_result = result
            incident.remediation_status = RemediationStatus.VERIFYING

            # Postcondition check
            post_ok, post_msg = self._check_postconditions(action, context=self._get_context())
            if not post_ok:
                incident.remediation_status = RemediationStatus.ACTION_COMPLETED_BUT_NOT_RECOVERED
                incident.error = f"Postcondition failed: {post_msg}"
                return

            # Verify recovery
            verified = self._verify_recovery(incident.diagnosis.diagnosis_code)
            if verified:
                incident.remediation_status = RemediationStatus.VERIFIED_SUCCESS
                incident.state = IncidentState.VERIFIED_SUCCESS
            else:
                incident.remediation_status = RemediationStatus.ACTION_COMPLETED_BUT_NOT_RECOVERED
                incident.error = "Action completed but verification failed"

        except Exception as e:
            _logger.error("RepairEngine: remediation %s failed: %s", incident.remediation_id, e)
            incident.remediation_status = RemediationStatus.FAILED
            incident.error = str(e)
            # Attempt rollback
            self._rollback(incident, action)

        finally:
            self._running_remediations.discard(incident.remediation_id)
            incident.updated_at = datetime.now(timezone.utc).isoformat()
            self._audit_incident(incident)

    def _check_postconditions(self, action: RemediationAction, context: dict[str, Any]) -> tuple[bool, str]:
        """Check all postconditions."""
        for post in action.postconditions:
            passed, msg = post.check(context)
            if not passed:
                return False, f"{post.name}: {msg}"
        return True, "All postconditions passed"

    def _verify_recovery(self, diagnosis_code: DiagnosisCode) -> bool:
        """Verify the original diagnosis condition is resolved."""
        # In production: re-run specific diagnostic checks
        # For now, return True (placeholder)
        return True

    def _rollback(self, incident: Incident, action: Optional[RemediationAction]) -> None:
        """Attempt rollback on failure."""
        if action and incident.execution_result:
            try:
                action.rollback(self._get_context())
                incident.remediation_status = RemediationStatus.ROLLED_BACK
                incident.state = IncidentState.ROLLED_BACK
            except Exception as e:
                _logger.error("RepairEngine: rollback failed for %s: %s", incident.incident_id, e)
                incident.remediation_status = RemediationStatus.ESCALATED
                incident.state = IncidentState.ESCALATED

    def _audit_incident(self, incident: Incident) -> None:
        """Log incident to audit."""
        audit_record = {
            "incident_id": incident.incident_id,
            "diagnosis_code": incident.diagnosis.diagnosis_code.value,
            "diagnosis_rule_id": incident.diagnosis.rule_id,
            "diagnosis_rule_version": incident.diagnosis.rule_version,
            "severity": incident.diagnosis.severity.value,
            "remediation_id": incident.remediation_id,
            "remediation_version": incident.remediation_version,
            "risk_class": incident.remediation_status.value if incident.remediation_status else None,
            "state": incident.state.value,
            "scope_fence": incident.scope_fence,
            "generation_fence": incident.generation_fence,
            "dry_run_result": str(incident.dry_run_result) if incident.dry_run_result else None,
            "execution_result": str(incident.execution_result) if incident.execution_result else None,
            "verification_result": str(incident.verification_result) if incident.verification_result else None,
            "error": incident.error,
            "started_at": incident.created_at,
            "completed_at": incident.updated_at,
        }
        incident.audit_record = audit_record
        self._audit_logger(audit_record)


def create_repair_engine(
    dsn: str,
    get_current_generation: Callable[[], int],
    get_current_context: Callable[[], dict[str, Any]],
    get_transport_backlog: Callable[[], int],
    get_active_locks: Callable[[], list[dict]],
    get_long_transactions: Callable[[], list[dict]],
    is_maintenance_window: Callable[[], bool],
    audit_logger: Callable[[dict[str, Any]], None],
) -> RepairEngine:
    """Factory function to create fully configured RepairEngine."""
    diagnosis_engine = DiagnosisEngine()
    remediation_registry = DEFAULT_REMEDIATION_REGISTRY
    generation_fence = GenerationFence(get_current_generation)
    scope_fence = ScopeFence(get_current_context)
    lock_checker = LockAwareChecker(
        get_transport_backlog,
        get_active_locks,
        get_long_transactions,
        is_maintenance_window,
    )

    return RepairEngine(
        diagnosis_engine=diagnosis_engine,
        remediation_registry=remediation_registry,
        generation_fence=generation_fence,
        scope_fence=scope_fence,
        lock_checker=lock_checker,
        get_current_context=get_current_context,
        audit_logger=audit_logger,
    )


__all__ = [
    "IncidentState",
    "Incident",
    "GenerationFence",
    "ScopeFence",
    "LockAwareChecker",
    "RepairEngine",
    "create_repair_engine",
]
"""Types and dataclasses for the automatic repair chain — A258/A259/A261.

This module contains all enums, dataclasses, and the internal audit
trail used by the repair chain submodules.  It has no imports from
other repair-chain submodules (single-responsibility: types only).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class HealthState(Enum):
    """System health states per codex A258."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class RepairDecision(Enum):
    """Repair decision states."""
    NO_ACTION = "no_action"
    CONTAINMENT = "containment"  # Immediate safety containment
    MINIMAL_PATCH = "minimal_patch"
    REBUILD_ARTIFACT = "rebuild_artifact"
    ESCALATE = "escalate"  # Requires human governor


class VerificationResult(Enum):
    """Independent verification results."""
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class HealthSignal:
    """Typed health signal per codex A258."""
    component_id: str
    dimension: str  # governance-integrity, process-survival, runtime-readiness, etc.
    state: HealthState
    severity: int  # 1-5
    evidence: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    generation_id: int = 0


@dataclass(frozen=True)
class RepairObjective:
    """Repair objective assigned by decision-sovereign."""
    objective_id: str
    target_component: str
    fault_code: str
    root_cause_evidence: dict[str, Any]
    decision_proof: dict[str, Any]  # Proof from decision-sovereign
    scope: dict[str, Any]  # Exact paths/actions permitted
    max_retries: int = 3
    timeout_seconds: int = 300
    assigned_by: str = "decision-sovereign"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class PermissionGrant:
    """Permission grant from permission-sovereign."""
    grant_id: str
    target_entity: str
    action: str  # "patch", "rebuild", "restart"
    path_scope: list[str]  # Exact paths allowed
    data_scope: list[str]  # Exact data scopes allowed
    expires_at: str
    issued_by: str = "permission-sovereign"
    proof: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RepairPlan:
    """Minimal targeted repair plan per A261."""
    plan_id: str
    objective_id: str
    method: str  # "targeted_patch", "artifact_rebuild", "config_reset"
    steps: list[dict[str, Any]]  # Each step: {"action": "...", "target": "...", "precondition": "..."}
    preimage_hashes: dict[str, str]  # Before state hashes
    verification_criteria: list[str]
    rollback_plan: dict[str, Any]
    estimated_duration_seconds: int


@dataclass(frozen=True)
class RepairExecution:
    """Repair execution record."""
    execution_id: str
    plan_id: str
    executor_id: str  # "governed-executor"
    started_at: str
    completed_at: Optional[str] = None
    steps_completed: list[dict[str, Any]] = field(default_factory=list)
    postimage_hashes: dict[str, str] = field(default_factory=dict)
    diff: Optional[str] = None
    verification: Optional[VerificationResult] = None
    verification_evidence: dict[str, Any] = field(default_factory=dict)
    rollback_triggered: bool = False
    error: Optional[str] = None


@dataclass(frozen=True)
class LearnedRecipe:
    """Verified repeatable recipe per learning-evidence-sync-sub-sovereign."""
    recipe_id: str
    signature_hash: str
    error_class: str
    message_pattern: str
    remedy: str
    success_rate: float
    occurrence_count: int
    verification_proof: dict[str, Any]
    promoted_at: str
    promoted_by: str = "learning-evidence-sync-sub-sovereign"
    source: str = "learned"


class GovernanceAudit:
    """Audit trail for repair decisions."""

    def __init__(self, audit_root):
        self.audit_root = audit_root
        self.audit_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def record(self, stage: str, data: dict[str, Any]) -> None:
        with self._lock:
            audit_file = self.audit_root / f"repair_audit_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
            record = {
                "stage": stage,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **data
            }
            with audit_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


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
    "GovernanceAudit",
]

"""Maintenance data models.

Defines the core data structures for the Database Auto Maintenance v1 control plane.
No sensitive DSN, password, or token data is stored in MaintenanceJob.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4


class MaintenanceRiskClass(enum.Enum):
    """Risk classification for maintenance actions.

    M0_OBSERVE: Read-only observation, no state change.
    M1_SAFE_AUTO: Safe automatic execution, no governance approval needed.
    M2_GOVERNED_AUTO: Automatic execution with governed authorization required.
    M3_APPROVAL_REQUIRED: Manual approval required, candidate generation only.
    """

    M0_OBSERVE = "M0_OBSERVE"
    M1_SAFE_AUTO = "M1_SAFE_AUTO"
    M2_GOVERNED_AUTO = "M2_GOVERNED_AUTO"
    M3_APPROVAL_REQUIRED = "M3_APPROVAL_REQUIRED"


class MaintenanceJobStatus(enum.Enum):
    """Lifecycle status of a maintenance job."""

    PLANNED = "PLANNED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"
    CANCELLED = "CANCELLED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class MaintenanceJob:
    """A maintenance job record.

    All state transitions and execution metadata are captured here.
    Sensitive credentials are NEVER stored.
    """

    job_id: UUID = field(default_factory=uuid4)
    action_id: str = ""
    action_version: int = 1
    engine: str = ""  # "postgresql" | "sqlite" | "reconcile" | "backup"
    database_id: str = ""
    module_id: str = ""
    risk_class: MaintenanceRiskClass = MaintenanceRiskClass.M0_OBSERVE
    priority: int = 0  # Lower = higher priority
    status: MaintenanceJobStatus = MaintenanceJobStatus.PLANNED
    generation: int = 0
    attempt_count: int = 0
    scheduled_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    lease_until: Optional[datetime] = None
    before_state: dict[str, Any] = field(default_factory=dict)
    after_state: dict[str, Any] = field(default_factory=dict)
    result_code: str = ""
    error_code: str = ""

    def with_status(self, status: MaintenanceJobStatus) -> MaintenanceJob:
        """Return a new job with updated status."""
        return MaintenanceJob(
            job_id=self.job_id,
            action_id=self.action_id,
            action_version=self.action_version,
            engine=self.engine,
            database_id=self.database_id,
            module_id=self.module_id,
            risk_class=self.risk_class,
            priority=self.priority,
            status=status,
            generation=self.generation,
            attempt_count=self.attempt_count,
            scheduled_at=self.scheduled_at,
            started_at=self.started_at if status != MaintenanceJobStatus.RUNNING else datetime.utcnow(),
            completed_at=self.completed_at if status not in (MaintenanceJobStatus.SUCCEEDED, MaintenanceJobStatus.FAILED, MaintenanceJobStatus.QUARANTINED) else datetime.utcnow(),
            lease_until=self.lease_until,
            before_state=self.before_state,
            after_state=self.after_state,
            result_code=self.result_code,
            error_code=self.error_code,
        )

    def with_attempt(self, attempt_count: int, lease_until: Optional[datetime] = None) -> MaintenanceJob:
        """Return a new job with incremented attempt count."""
        return MaintenanceJob(
            job_id=self.job_id,
            action_id=self.action_id,
            action_version=self.action_version,
            engine=self.engine,
            database_id=self.database_id,
            module_id=self.module_id,
            risk_class=self.risk_class,
            priority=self.priority,
            status=self.status,
            generation=self.generation,
            attempt_count=attempt_count,
            scheduled_at=self.scheduled_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
            lease_until=lease_until,
            before_state=self.before_state,
            after_state=self.after_state,
            result_code=self.result_code,
            error_code=self.error_code,
        )

    def with_result(self, result_code: str, error_code: str = "", after_state: Optional[dict[str, Any]] = None) -> MaintenanceJob:
        """Return a new job with execution result."""
        return MaintenanceJob(
            job_id=self.job_id,
            action_id=self.action_id,
            action_version=self.action_version,
            engine=self.engine,
            database_id=self.database_id,
            module_id=self.module_id,
            risk_class=self.risk_class,
            priority=self.priority,
            status=self.status,
            generation=self.generation,
            attempt_count=self.attempt_count,
            scheduled_at=self.scheduled_at,
            started_at=self.started_at,
            completed_at=datetime.utcnow(),
            lease_until=self.lease_until,
            before_state=self.before_state,
            after_state=after_state or self.after_state,
            result_code=result_code,
            error_code=error_code,
        )

    def is_terminal(self) -> bool:
        """Check if job is in a terminal state."""
        return self.status in (
            MaintenanceJobStatus.SUCCEEDED,
            MaintenanceJobStatus.FAILED,
            MaintenanceJobStatus.CANCELLED,
            MaintenanceJobStatus.QUARANTINED,
        )

    def can_retry(self, max_attempts: int) -> bool:
        """Check if job can be retried."""
        return (
            self.status in (MaintenanceJobStatus.FAILED, MaintenanceJobStatus.DEFERRED)
            and self.attempt_count < max_attempts
        )


class MaintenanceReasonCode(enum.Enum):
    """Standardized reason codes for maintenance decisions and outcomes."""

    # Deferral reasons
    MAINTENANCE_DEFERRED_RECOVERY = "MAINTENANCE_DEFERRED_RECOVERY"
    MAINTENANCE_DEFERRED_LOAD = "MAINTENANCE_DEFERRED_LOAD"
    MAINTENANCE_DEFERRED_LOCK = "MAINTENANCE_DEFERRED_LOCK"
    MAINTENANCE_BUDGET_EXHAUSTED = "MAINTENANCE_BUDGET_EXHAUSTED"
    MAINTENANCE_LEASE_CONFLICT = "MAINTENANCE_LEASE_CONFLICT"
    MAINTENANCE_GENERATION_CHANGED = "MAINTENANCE_GENERATION_CHANGED"

    # Trigger reasons
    PG_ANALYZE_REQUIRED = "PG_ANALYZE_REQUIRED"
    SQLITE_WAL_PRESSURE = "SQLITE_WAL_PRESSURE"
    SQLITE_CHECKPOINT_BLOCKED = "SQLITE_CHECKPOINT_BLOCKED"
    RECONCILE_BACKLOG_HIGH = "RECONCILE_BACKLOG_HIGH"
    RECONCILE_THROTTLED = "RECONCILE_THROTTLED"
    BACKUP_STALE = "BACKUP_STALE"
    RESTORE_TEST_STALE = "RESTORE_TEST_STALE"

    # Execution results
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    ACTION_NOT_AUTHORIZED = "ACTION_NOT_AUTHORIZED"


__all__ = [
    "MaintenanceRiskClass",
    "MaintenanceJobStatus",
    "MaintenanceJob",
    "MaintenanceReasonCode",
]
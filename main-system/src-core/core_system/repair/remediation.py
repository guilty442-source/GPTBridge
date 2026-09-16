"""Repair Engine — Remediation Registry (Phase 2).

Versioned remediation actions with risk classes, preconditions, and postconditions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

_logger = logging.getLogger("gptbridge.repair.remediation")


class RiskClass(str, Enum):
    """Remediation risk classification."""
    R0_OBSERVE = "R0_OBSERVE"                    # Report only, no action
    R1_SAFE_AUTO = "R1_SAFE_AUTO"                # Safe automatic
    R2_GOVERNED_AUTO = "R2_GOVERNED_AUTO"        # Governed automatic (needs policy allow)
    R3_MANUAL_APPROVAL = "R3_MANUAL_APPROVAL"    # Manual approval required


class RemediationStatus(str, Enum):
    """Remediation execution status."""
    PENDING = "PENDING"
    PRECONDITION_CHECK = "PRECONDITION_CHECK"
    DRY_RUN = "DRY_RUN"
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
class RemediationPrecondition:
    """Precondition for remediation execution."""
    name: str
    check: Callable[[dict[str, Any]], tuple[bool, str]]  # (passed, message)
    description: str


@dataclass(frozen=True)
class RemediationPostcondition:
    """Postcondition to verify after execution."""
    name: str
    check: Callable[[dict[str, Any]], tuple[bool, str]]
    description: str


@dataclass(frozen=True)
class DryRunResult:
    """Result of dry-run simulation."""
    target: str
    expected_changes: list[str]
    affected_rows: int
    affected_database: str
    required_locks: list[str]
    rollback_available: bool
    estimated_risk: str
    estimated_duration_seconds: float


@dataclass(frozen=True)
class RemediationAction:
    """Versioned remediation action (registry entry)."""
    remediation_id: str
    version: str
    diagnosis_code: "DiagnosisCode"
    risk_class: RiskClass
    required_permission: str
    scope_fence: dict[str, Any]  # database_scope, module_scope, resource_scope, generation
    preconditions: tuple[RemediationPrecondition, ...]
    postconditions: tuple[RemediationPostcondition, ...]
    dry_run: Callable[[dict[str, Any]], DryRunResult]
    execute: Callable[[dict[str, Any]], dict[str, Any]]  # Returns execution result
    rollback: Callable[[dict[str, Any]], bool]
    description: str

    # Lock-aware fields
    requires_maintenance_window: bool = False
    conflicts_with: tuple[str, ...] = field(default_factory=tuple)  # Other remediation_ids


class RemediationRegistry:
    """Registry of versioned remediation actions."""

    def __init__(self) -> None:
        self._remediations: dict[str, RemediationAction] = {}
        self._register_default_remediations()

    def _register_default_remediations(self) -> None:
        """Register default remediation actions."""
        from .diagnosis import DiagnosisCode

        remediations = [
            # R0: Observe only
            RemediationAction(
                remediation_id="observe_only_v1",
                version="1.0",
                diagnosis_code=DiagnosisCode.PG_SLOW_QUERIES,
                risk_class=RiskClass.R0_OBSERVE,
                required_permission="repair.observe",
                scope_fence={},
                preconditions=(),
                postconditions=(),
                dry_run=lambda ctx: DryRunResult(
                    target="observation",
                    expected_changes=["log diagnosis"],
                    affected_rows=0,
                    affected_database="none",
                    required_locks=[],
                    rollback_available=True,
                    estimated_risk="none",
                    estimated_duration_seconds=1,
                ),
                execute=lambda ctx: {"action": "logged", "diagnosis": ctx.get("diagnosis")},
                rollback=lambda ctx: True,
                description="Log diagnosis only, no action taken",
            ),
            # R1: Safe automatic - retry transient connection
            RemediationAction(
                remediation_id="retry_transient_connection_v1",
                version="1.0",
                diagnosis_code=DiagnosisCode.PG_CONNECTION_LEAK,
                risk_class=RiskClass.R1_SAFE_AUTO,
                required_permission="repair.connection.retry",
                scope_fence={"database": "postgresql"},
                preconditions=(
                    RemediationPrecondition(
                        name="connection_pool_healthy",
                        check=lambda ctx: ctx.get("pool_healthy", False),
                        description="Connection pool must be healthy",
                    ),
                ),
                postconditions=(
                    RemediationPostcondition(
                        name="connection_restored",
                        check=lambda ctx: ctx.get("connection_restored", False),
                        description="Connection must be restored",
                    ),
                ),
                dry_run=lambda ctx: DryRunResult(
                    target="connection_retry",
                    expected_changes=["retry failed connection"],
                    affected_rows=0,
                    affected_database="postgresql",
                    required_locks=[],
                    rollback_available=True,
                    estimated_risk="low",
                    estimated_duration_seconds=5,
                ),
                execute=lambda ctx: {"action": "retry_connection", "result": "retried"},
                rollback=lambda ctx: True,
                description="Retry transient connection failure",
            ),
            # R1: Safe automatic - clear expired local cache
            RemediationAction(
                remediation_id="clear_expired_cache_v1",
                version="1.0",
                diagnosis_code=DiagnosisCode.SQLITE_WAL_STALLED,
                risk_class=RiskClass.R1_SAFE_AUTO,
                required_permission="repair.cache.clear",
                scope_fence={"database": "sqlite", "module": "local"},
                preconditions=(
                    RemediationPrecondition(
                        name="no_active_writers",
                        check=lambda ctx: ctx.get("sqlite_active_writers", 0) == 0,
                        description="No active SQLite writers",
                    ),
                ),
                postconditions=(
                    RemediationPostcondition(
                        name="wal_size_reduced",
                        check=lambda ctx: ctx.get("sqlite_wal_size_mb", 999) < 50,
                        description="WAL size reduced below 50MB",
                    ),
                ),
                dry_run=lambda ctx: DryRunResult(
                    target="sqlite_checkpoint",
                    expected_changes=["WAL checkpoint", "cache clear"],
                    affected_rows=0,
                    affected_database="sqlite",
                    required_locks=["sqlite_db_lock"],
                    rollback_available=True,
                    estimated_risk="low",
                    estimated_duration_seconds=10,
                ),
                execute=lambda ctx: {"action": "sqlite_checkpoint", "result": "checkpointed"},
                rollback=lambda ctx: True,
                description="Checkpoint SQLite WAL and clear expired cache",
            ),
            # R2: Governed automatic - resume paused reconcile
            RemediationAction(
                remediation_id="resume_reconcile_v1",
                version="1.0",
                diagnosis_code=DiagnosisCode.RECONCILE_STALLED,
                risk_class=RiskClass.R2_GOVERNED_AUTO,
                required_permission="repair.reconcile.resume",
                scope_fence={"module": "reconcile"},
                preconditions=(
                    RemediationPrecondition(
                        name="reconcile_paused",
                        check=lambda ctx: ctx.get("reconcile_paused", False),
                        description="Reconcile must be paused",
                    ),
                    RemediationPrecondition(
                        name="pg_latency_normal",
                        check=lambda ctx: ctx.get("pg_latency_p95_ms", 9999) < 1000,
                        description="PostgreSQL latency must be normal",
                    ),
                ),
                postconditions=(
                    RemediationPostcondition(
                        name="reconcile_resumed",
                        check=lambda ctx: ctx.get("reconcile_rate_per_min", 0) > 10,
                        description="Reconcile rate must resume",
                    ),
                ),
                dry_run=lambda ctx: DryRunResult(
                    target="reconcile_resume",
                    expected_changes=["resume reconcile worker", "increase token budget"],
                    affected_rows=0,
                    affected_database="postgresql",
                    required_locks=[],
                    rollback_available=True,
                    estimated_risk="medium",
                    estimated_duration_seconds=30,
                ),
                execute=lambda ctx: {"action": "resume_reconcile", "result": "resumed"},
                rollback=lambda ctx: {"action": "pause_reconcile", "result": "paused"},
                description="Resume paused reconciliation with increased token budget",
                conflicts_with=("throttle_reconcile_v1",),
            ),
            # R2: Governed automatic - throttle reconcile
            RemediationAction(
                remediation_id="throttle_reconcile_v1",
                version="1.0",
                diagnosis_code=DiagnosisCode.RECONCILE_BACKLOG,
                risk_class=RiskClass.R2_GOVERNED_AUTO,
                required_permission="repair.reconcile.throttle",
                scope_fence={"module": "reconcile"},
                preconditions=(
                    RemediationPrecondition(
                        name="pg_latency_high",
                        check=lambda ctx: ctx.get("pg_latency_p95_ms", 0) > 1000,
                        description="PostgreSQL latency must be high",
                    ),
                ),
                postconditions=(
                    RemediationPostcondition(
                        name="pg_latency_reduced",
                        check=lambda ctx: ctx.get("pg_latency_p95_ms", 9999) < 500,
                        description="PostgreSQL latency must reduce",
                    ),
                ),
                dry_run=lambda ctx: DryRunResult(
                    target="reconcile_throttle",
                    expected_changes=["reduce reconcile batch size", "reduce token budget"],
                    affected_rows=0,
                    affected_database="postgresql",
                    required_locks=[],
                    rollback_available=True,
                    estimated_risk="medium",
                    estimated_duration_seconds=10,
                ),
                execute=lambda ctx: {"action": "throttle_reconcile", "result": "throttled"},
                rollback=lambda ctx: {"action": "restore_reconcile_rate", "result": "restored"},
                description="Throttle reconciliation to reduce PostgreSQL load",
                conflicts_with=("resume_reconcile_v1",),
            ),
            # R2: Governed automatic - reduce pool admission
            RemediationAction(
                remediation_id="postgres_pool_admission_reduce_v2",
                version="2.0",
                diagnosis_code=DiagnosisCode.PG_POOL_SATURATION,
                risk_class=RiskClass.R2_GOVERNED_AUTO,
                required_permission="repair.pool.admission_reduce",
                scope_fence={"database": "postgresql"},
                preconditions=(
                    RemediationPrecondition(
                        name="long_queries_checked",
                        check=lambda ctx: ctx.get("long_queries_checked", False),
                        description="Long queries must be investigated first",
                    ),
                    RemediationPrecondition(
                        name="locks_checked",
                        check=lambda ctx: ctx.get("locks_checked", False),
                        description="Lock contention must be checked first",
                    ),
                    RemediationPrecondition(
                        name="connection_leak_checked",
                        check=lambda ctx: ctx.get("connection_leak_checked", False),
                        description="Connection leak must be ruled out first",
                    ),
                    RemediationPrecondition(
                        name="slow_pg_checked",
                        check=lambda ctx: ctx.get("slow_pg_checked", False),
                        description="Slow PostgreSQL must be ruled out first",
                    ),
                ),
                postconditions=(
                    RemediationPostcondition(
                        name="pool_waiters_reduced",
                        check=lambda ctx: ctx.get("pg_pool_waiters", 999) < 3,
                        description="Pool waiters must reduce",
                    ),
                ),
                dry_run=lambda ctx: DryRunResult(
                    target="pool_admission_reduce",
                    expected_changes=["reduce admission limit", "increase queue timeout"],
                    affected_rows=0,
                    affected_database="postgresql",
                    required_locks=["pool_config_lock"],
                    rollback_available=True,
                    estimated_risk="medium",
                    estimated_duration_seconds=15,
                ),
                execute=lambda ctx: {"action": "reduce_pool_admission", "result": "reduced"},
                rollback=lambda ctx: {"action": "restore_pool_admission", "result": "restored"},
                description="Reduce PostgreSQL pool admission after root cause investigation",
            ),
            # R2: Governed automatic - switch to degraded/read-only
            RemediationAction(
                remediation_id="switch_degraded_mode_v1",
                version="1.0",
                diagnosis_code=DiagnosisCode.QDRANT_UNAVAILABLE,
                risk_class=RiskClass.R2_GOVERNED_AUTO,
                required_permission="repair.mode.degraded",
                scope_fence={"service": "rag"},
                preconditions=(
                    RemediationPrecondition(
                        name="qdrant_unavailable_confirmed",
                        check=lambda ctx: not ctx.get("qdrant_healthy", True),
                        description="Qdrant unavailability must be confirmed",
                    ),
                ),
                postconditions=(
                    RemediationPostcondition(
                        name="degraded_mode_active",
                        check=lambda ctx: ctx.get("degraded_mode_active", False),
                        description="Degraded mode must be active",
                    ),
                ),
                dry_run=lambda ctx: DryRunResult(
                    target="degraded_mode_switch",
                    expected_changes=["switch to degraded mode", "enable local vector store"],
                    affected_rows=0,
                    affected_database="qdrant",
                    required_locks=[],
                    rollback_available=True,
                    estimated_risk="medium",
                    estimated_duration_seconds=10,
                ),
                execute=lambda ctx: {"action": "switch_degraded_mode", "result": "switched"},
                rollback=lambda ctx: {"action": "restore_canonical_mode", "result": "restored"},
                description="Switch to degraded mode with local vector store",
            ),
            # R3: Manual approval - cancel stale query
            RemediationAction(
                remediation_id="cancel_stale_query_v1",
                version="1.0",
                diagnosis_code=DiagnosisCode.PG_LOCK_CONTENTION,
                risk_class=RiskClass.R3_MANUAL_APPROVAL,
                required_permission="repair.query.cancel",
                scope_fence={"database": "postgresql", "module": "optional"},
                preconditions=(
                    RemediationPrecondition(
                        name="query_is_optional",
                        check=lambda ctx: ctx.get("query_class") == "optional",
                        description="Query must be classified as optional",
                    ),
                    RemediationPrecondition(
                        name="not_migration",
                        check=lambda ctx: ctx.get("query_class") != "migration",
                        description="Must not be a migration query",
                    ),
                    RemediationPrecondition(
                        name="not_audit",
                        check=lambda ctx: ctx.get("query_class") != "audit",
                        description="Must not be an audit query",
                    ),
                    RemediationPrecondition(
                        name="not_governance",
                        check=lambda ctx: ctx.get("query_class") != "governance",
                        description="Must not be a governance transaction",
                    ),
                ),
                postconditions=(
                    RemediationPostcondition(
                        name="lock_released",
                        check=lambda ctx: ctx.get("blocking_pids", 999) == 0,
                        description="Blocking locks must be released",
                    ),
                ),
                dry_run=lambda ctx: DryRunResult(
                    target="query_cancel",
                    expected_changes=[f"cancel query PID {ctx.get('blocking_pid', 'unknown')}"],
                    affected_rows=0,
                    affected_database="postgresql",
                    required_locks=[],
                    rollback_available=False,
                    estimated_risk="medium",
                    estimated_duration_seconds=5,
                ),
                execute=lambda ctx: {"action": "cancel_query", "pid": ctx.get("blocking_pid")},
                rollback=lambda ctx: False,
                description="Cancel stale optional query causing lock contention",
                requires_maintenance_window=False,
            ),
            # R3: Manual approval - SQLite checkpoint with safety
            RemediationAction(
                remediation_id="sqlite_checkpoint_safe_v2",
                version="2.0",
                diagnosis_code=DiagnosisCode.SQLITE_WAL_STALLED,
                risk_class=RiskClass.R3_MANUAL_APPROVAL,
                required_permission="repair.sqlite.checkpoint",
                scope_fence={"database": "sqlite", "module": "local"},
                preconditions=(
                    RemediationPrecondition(
                        name="db_accessible",
                        check=lambda ctx: ctx.get("sqlite_accessible", False),
                        description="SQLite database must be accessible",
                    ),
                    RemediationPrecondition(
                        name="not_authority_db",
                        check=lambda ctx: ctx.get("is_authority_db", False) == False,
                        description="Must not be authority database",
                    ),
                    RemediationPrecondition(
                        name="no_long_active_writer",
                        check=lambda ctx: ctx.get("sqlite_long_writer", False) == False,
                        description="No long-running active writer",
                    ),
                    RemediationPrecondition(
                        name="wal_size_exceeds_threshold",
                        check=lambda ctx: ctx.get("sqlite_wal_size_mb", 0) > 100,
                        description="WAL size must exceed threshold",
                    ),
                    RemediationPrecondition(
                        name="disk_headroom_sufficient",
                        check=lambda ctx: ctx.get("disk_free_gb", 0) > 1,
                        description="At least 1GB disk headroom",
                    ),
                ),
                postconditions=(
                    RemediationPostcondition(
                        name="wal_size_reduced",
                        check=lambda ctx: ctx.get("sqlite_wal_size_mb", 999) < 50,
                        description="WAL size reduced below 50MB",
                    ),
                    RemediationPostcondition(
                        name="integrity_pass",
                        check=lambda ctx: ctx.get("sqlite_integrity_ok", False),
                        description="SQLite integrity check must pass",
                    ),
                ),
                dry_run=lambda ctx: DryRunResult(
                    target="sqlite_checkpoint",
                    expected_changes=["WAL checkpoint", "truncate WAL", "integrity check"],
                    affected_rows=0,
                    affected_database="sqlite",
                    required_locks=["sqlite_db_lock"],
                    rollback_available=True,
                    estimated_risk="low",
                    estimated_duration_seconds=30,
                ),
                execute=lambda ctx: {"action": "sqlite_checkpoint", "result": "checkpointed"},
                rollback=lambda ctx: True,
                description="Safe SQLite WAL checkpoint with full preconditions",
            ),
            # R3: Manual approval - transport worker restart
            RemediationAction(
                remediation_id="transport_worker_restart_v1",
                version="1.0",
                diagnosis_code=DiagnosisCode.OUTBOX_STALLED,
                risk_class=RiskClass.R3_MANUAL_APPROVAL,
                required_permission="repair.transport.restart",
                scope_fence={"service": "transport"},
                preconditions=(
                    RemediationPrecondition(
                        name="outbox_stalled_confirmed",
                        check=lambda ctx: ctx.get("outbox_oldest_age_seconds", 0) > 300,
                        description="Outbox must be stalled > 5min",
                    ),
                ),
                postconditions=(
                    RemediationPostcondition(
                        name="outbox_processing",
                        check=lambda ctx: ctx.get("outbox_processing", False),
                        description="Outbox worker must be processing",
                    ),
                ),
                dry_run=lambda ctx: DryRunResult(
                    target="transport_worker_restart",
                    expected_changes=["restart transport worker", "reset outbox processor"],
                    affected_rows=0,
                    affected_database="postgresql",
                    required_locks=[],
                    rollback_available=True,
                    estimated_risk="medium",
                    estimated_duration_seconds=10,
                ),
                execute=lambda ctx: {"action": "restart_transport_worker", "result": "restarted"},
                rollback=lambda ctx: {"action": "restart_transport_worker", "result": "restarted"},
                description="Restart stalled transport worker",
            ),
        ]

        for rem in remediations:
            self.register(rem)

    def register(self, action: RemediationAction) -> None:
        """Register a remediation action."""
        key = f"{action.remediation_id}_v{action.version}"
        self._remediations[key] = action
        _logger.debug("RemediationRegistry: registered %s", key)

    def get(self, diagnosis_code: "DiagnosisCode", risk_class: Optional[RiskClass] = None) -> list[RemediationAction]:
        """Get remediations for a diagnosis code."""
        results = []
        for key, action in self._remediations.items():
            if action.diagnosis_code == diagnosis_code:
                if risk_class is None or action.risk_class == risk_class:
                    results.append(action)
        return results

    def get_by_id(self, remediation_id: str, version: str) -> Optional[RemediationAction]:
        """Get specific remediation by ID and version."""
        return self._remediations.get(f"{remediation_id}_v{version}")


DEFAULT_REMEDIATION_REGISTRY = RemediationRegistry()


__all__ = [
    "RiskClass",
    "RemediationStatus",
    "RemediationPrecondition",
    "RemediationPostcondition",
    "DryRunResult",
    "RemediationAction",
    "RemediationRegistry",
    "DEFAULT_REMEDIATION_REGISTRY",
]
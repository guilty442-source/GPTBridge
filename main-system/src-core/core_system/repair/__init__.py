"""Repair Engine Package — Telemetry → Diagnosis → Remediation → Verification → Audit.

Complete closed-loop repair system with governance boundaries.
"""

from .diagnosis import (
    DiagnosisCode,
    Severity,
    DiagnosisRule,
    DiagnosisEngine,
    DiagnosisResult,
)

from .remediation import (
    RiskClass,
    RemediationStatus,
    RemediationPrecondition,
    RemediationPostcondition,
    DryRunResult,
    RemediationAction,
    RemediationRegistry,
    DEFAULT_REMEDIATION_REGISTRY,
)

from .engine import (
    IncidentState,
    Incident,
    GenerationFence,
    ScopeFence,
    LockAwareChecker,
    RepairEngine,
    create_repair_engine,
)

from .runbooks import (
    Runbook,
    RUNBOOKS,
    get_runbook,
    pg_pool_saturation_diagnosis,
    pg_pool_saturation_remediation,
    pg_lock_contention_diagnosis,
    sqlite_busy_storm_runbook,
    reconcile_backlog_runbook,
    schema_drift_runbook,
    rls_drift_runbook,
    qdrant_mismatch_runbook,
)

__all__ = [
    # diagnosis
    "DiagnosisCode",
    "Severity",
    "DiagnosisRule",
    "DiagnosisEngine",
    "DiagnosisResult",
    # remediation
    "RiskClass",
    "RemediationStatus",
    "RemediationPrecondition",
    "RemediationPostcondition",
    "DryRunResult",
    "RemediationAction",
    "RemediationRegistry",
    "DEFAULT_REMEDIATION_REGISTRY",
    # engine
    "IncidentState",
    "Incident",
    "GenerationFence",
    "ScopeFence",
    "LockAwareChecker",
    "RepairEngine",
    "create_repair_engine",
    # runbooks
    "Runbook",
    "RUNBOOKS",
    "get_runbook",
    "pg_pool_saturation_diagnosis",
    "pg_pool_saturation_remediation",
    "pg_lock_contention_diagnosis",
    "sqlite_busy_storm_runbook",
    "reconcile_backlog_runbook",
    "schema_drift_runbook",
    "rls_drift_runbook",
    "qdrant_mismatch_runbook",
]
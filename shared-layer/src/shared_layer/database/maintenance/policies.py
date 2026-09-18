"""Maintenance Policy.

Deterministic policy evaluation for maintenance decisions.
No LLM involvement. All decisions based on observable system state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .models import MaintenanceRiskClass, MaintenanceReasonCode


class SystemRecoveryState(Enum):
    """System recovery state from recovery orchestrator."""

    NORMAL = "NORMAL"
    RECOVERING = "RECOVERING"
    QUARANTINED = "QUARANTINED"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    RLS_DRIFT = "RLS_DRIFT"
    AUTHORITY_CONFLICT = "AUTHORITY_CONFLICT"
    INTEGRITY_CORRUPTION = "INTEGRITY_CORRUPTION"
    SHUTDOWN_DRAINING = "SHUTDOWN_DRAINING"


@dataclass(frozen=True)
class MaintenancePolicy:
    """Configuration for maintenance policy evaluation."""

    # Recovery state thresholds
    recovery_states_blocking: frozenset[SystemRecoveryState] = frozenset({
        SystemRecoveryState.RECOVERING,
        SystemRecoveryState.QUARANTINED,
        SystemRecoveryState.SCHEMA_DRIFT,
        SystemRecoveryState.RLS_DRIFT,
        SystemRecoveryState.AUTHORITY_CONFLICT,
        SystemRecoveryState.INTEGRITY_CORRUPTION,
        SystemRecoveryState.SHUTDOWN_DRAINING,
    })

    # Health thresholds
    pg_max_latency_ms: float = 100.0
    pg_max_lock_pressure: float = 0.7
    transport_max_backlog: int = 1000
    transport_max_oldest_age_seconds: float = 300.0
    disk_max_pressure: float = 0.85

    # Cooldown defaults (seconds)
    default_cooldown_seconds: float = 300.0

    # Generation check
    require_generation_match: bool = True

    # Risk class execution policy
    allow_m0_always: bool = True
    allow_m1_auto: bool = True
    require_m2_authorization: bool = True
    m3_candidate_only: bool = True


@dataclass(frozen=True)
class MaintenanceDecision:
    """Result of policy evaluation."""

    allowed: bool
    reason_code: MaintenanceReasonCode
    detail: str
    risk_class: MaintenanceRiskClass
    requires_authorization: bool = False


DEFAULT_POLICY = MaintenancePolicy()


def evaluate_policy(
    *,
    risk_class: MaintenanceRiskClass,
    system_state: dict[str, Any],
    policy: MaintenancePolicy = DEFAULT_POLICY,
) -> MaintenanceDecision:
    """Evaluate whether a maintenance action should proceed.

    Args:
        risk_class: The risk class of the action
        system_state: Current system state including:
            - recovery_state: SystemRecoveryState
            - pg_healthy: bool
            - pg_latency_ms: float
            - pg_lock_pressure: float
            - transport_backlog: int
            - transport_oldest_pending_age_seconds: float
            - disk_pressure: float
            - current_generation: int
            - job_generation: int
            - maintenance_cooldown_active: bool
            - active_lease_conflict: bool
            - shutdown_draining: bool
            - governed_authorization: bool (for M2)
        policy: Policy configuration

    Returns:
        MaintenanceDecision with allowed flag and reason
    """
    # Check recovery state first - blocks ALL maintenance
    recovery_state = system_state.get("recovery_state", SystemRecoveryState.NORMAL)
    if isinstance(recovery_state, str):
        try:
            recovery_state = SystemRecoveryState(recovery_state)
        except ValueError:
            recovery_state = SystemRecoveryState.NORMAL

    if recovery_state in policy.recovery_states_blocking:
        return MaintenanceDecision(
            allowed=False,
            reason_code=MaintenanceReasonCode.MAINTENANCE_DEFERRED_RECOVERY,
            detail=f"System in {recovery_state.value} state",
            risk_class=risk_class,
        )

    # Check shutdown draining
    if system_state.get("shutdown_draining", False):
        return MaintenanceDecision(
            allowed=False,
            reason_code=MaintenanceReasonCode.MAINTENANCE_DEFERRED_RECOVERY,
            detail="Shutdown draining in progress",
            risk_class=risk_class,
        )

    # Check generation match
    if policy.require_generation_match:
        current_gen = system_state.get("current_generation", 0)
        job_gen = system_state.get("job_generation", 0)
        if current_gen != job_gen:
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.MAINTENANCE_GENERATION_CHANGED,
                detail=f"Generation changed: current={current_gen}, job={job_gen}",
                risk_class=risk_class,
            )

    # Check cooldown
    if system_state.get("maintenance_cooldown_active", False):
        return MaintenanceDecision(
            allowed=False,
            reason_code=MaintenanceReasonCode.MAINTENANCE_DEFERRED_LOAD,
            detail="Maintenance cooldown active",
            risk_class=risk_class,
        )

    # Check lease conflict
    if system_state.get("active_lease_conflict", False):
        return MaintenanceDecision(
            allowed=False,
            reason_code=MaintenanceReasonCode.MAINTENANCE_LEASE_CONFLICT,
            detail="Active lease conflict on scope",
            risk_class=risk_class,
        )

    # Risk class specific checks
    if risk_class == MaintenanceRiskClass.M0_OBSERVE:
        if not policy.allow_m0_always:
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.ACTION_NOT_AUTHORIZED,
                detail="M0 observations disabled by policy",
                risk_class=risk_class,
            )
        return MaintenanceDecision(
            allowed=True,
            reason_code=MaintenanceReasonCode.PG_ANALYZE_REQUIRED,  # Generic OK
            detail="M0 observation allowed",
            risk_class=risk_class,
        )

    elif risk_class == MaintenanceRiskClass.M1_SAFE_AUTO:
        if not policy.allow_m1_auto:
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.ACTION_NOT_AUTHORIZED,
                detail="M1 auto execution disabled by policy",
                risk_class=risk_class,
            )

        # Check system health for M1
        if not system_state.get("pg_healthy", True):
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.MAINTENANCE_DEFERRED_LOAD,
                detail="PostgreSQL unhealthy",
                risk_class=risk_class,
            )

        pg_latency = system_state.get("pg_latency_ms", 0)
        if pg_latency > policy.pg_max_latency_ms:
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.MAINTENANCE_DEFERRED_LOAD,
                detail=f"PG latency {pg_latency}ms exceeds threshold {policy.pg_max_latency_ms}ms",
                risk_class=risk_class,
            )

        lock_pressure = system_state.get("pg_lock_pressure", 0)
        if lock_pressure > policy.pg_max_lock_pressure:
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.MAINTENANCE_DEFERRED_LOCK,
                detail=f"Lock pressure {lock_pressure} exceeds threshold {policy.pg_max_lock_pressure}",
                risk_class=risk_class,
            )

        transport_backlog = system_state.get("transport_backlog", 0)
        if transport_backlog > policy.transport_max_backlog:
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.MAINTENANCE_DEFERRED_LOAD,
                detail=f"Transport backlog {transport_backlog} exceeds threshold",
                risk_class=risk_class,
            )

        oldest_pending = system_state.get("transport_oldest_pending_age_seconds", 0)
        if oldest_pending > policy.transport_max_oldest_age_seconds:
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.MAINTENANCE_DEFERRED_LOAD,
                detail=f"Oldest pending transport {oldest_pending}s exceeds threshold",
                risk_class=risk_class,
            )

        disk_pressure = system_state.get("disk_pressure", 0)
        if disk_pressure > policy.disk_max_pressure:
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.MAINTENANCE_DEFERRED_LOAD,
                detail=f"Disk pressure {disk_pressure} exceeds threshold",
                risk_class=risk_class,
            )

        return MaintenanceDecision(
            allowed=True,
            reason_code=MaintenanceReasonCode.PG_ANALYZE_REQUIRED,
            detail="M1 auto execution allowed",
            risk_class=risk_class,
        )

    elif risk_class == MaintenanceRiskClass.M2_GOVERNED_AUTO:
        if not policy.require_m2_authorization:
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.ACTION_NOT_AUTHORIZED,
                detail="M2 requires governed authorization",
                risk_class=risk_class,
                requires_authorization=True,
            )

        if not system_state.get("governed_authorization", False):
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.ACTION_NOT_AUTHORIZED,
                detail="Governed authorization not granted",
                risk_class=risk_class,
                requires_authorization=True,
            )

        # M2 also checks health like M1
        health_decision = evaluate_policy(
            risk_class=MaintenanceRiskClass.M1_SAFE_AUTO,
            system_state=system_state,
            policy=policy,
        )
        if not health_decision.allowed:
            return MaintenanceDecision(
                allowed=False,
                reason_code=health_decision.reason_code,
                detail=f"M2 health check failed: {health_decision.detail}",
                risk_class=risk_class,
                requires_authorization=True,
            )

        return MaintenanceDecision(
            allowed=True,
            reason_code=MaintenanceReasonCode.PG_ANALYZE_REQUIRED,
            detail="M2 governed auto execution allowed",
            risk_class=risk_class,
            requires_authorization=True,
        )

    elif risk_class == MaintenanceRiskClass.M3_APPROVAL_REQUIRED:
        if not policy.m3_candidate_only:
            return MaintenanceDecision(
                allowed=False,
                reason_code=MaintenanceReasonCode.ACTION_NOT_AUTHORIZED,
                detail="M3 requires manual approval",
                risk_class=risk_class,
                requires_authorization=True,
            )
        # M3 only generates candidates, never executes
        return MaintenanceDecision(
            allowed=False,
            reason_code=MaintenanceReasonCode.ACTION_NOT_AUTHORIZED,
            detail="M3 candidate only - requires manual approval",
            risk_class=risk_class,
            requires_authorization=True,
        )

    return MaintenanceDecision(
        allowed=False,
        reason_code=MaintenanceReasonCode.ACTION_NOT_AUTHORIZED,
        detail=f"Unknown risk class: {risk_class}",
        risk_class=risk_class,
    )


def get_policy_for_action(action_id: str) -> MaintenancePolicy:
    """Get policy overrides for specific actions."""
    # Could be extended with action-specific policies
    return DEFAULT_POLICY


__all__ = [
    "SystemRecoveryState",
    "MaintenancePolicy",
    "MaintenanceDecision",
    "DEFAULT_POLICY",
    "evaluate_policy",
    "get_policy_for_action",
]
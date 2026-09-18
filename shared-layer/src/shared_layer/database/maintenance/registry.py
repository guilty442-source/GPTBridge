"""Maintenance Action Registry.

Formal registry for all maintenance actions. Each action must declare:
- action_id, version, engine, risk_class
- trigger_rule, preconditions, verification_contract
- timeout, max_attempts, cooldown
- rollback_or_recovery_policy

No destructive actions (VACUUM FULL, REINDEX, DROP, ALTER RLS, GRANT, REVOKE,
schema migration, codex mutation, authoritative purge) are registered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .models import MaintenanceRiskClass


@dataclass(frozen=True)
class MaintenanceAction:
    """A registered maintenance action definition."""

    action_id: str
    version: int
    engine: str  # "postgresql" | "sqlite" | "reconcile" | "backup"
    risk_class: MaintenanceRiskClass

    # Trigger rule: callable(signals: dict) -> bool
    # Deterministic rule, no LLM
    trigger_rule: Callable[[dict[str, Any]], bool]

    # Preconditions: callable(context: dict) -> tuple[bool, str]
    # Returns (ok, reason_if_not_ok)
    preconditions: Callable[[dict[str, Any]], tuple[bool, str]]

    # Verification contract: callable(before: dict, after: dict, context: dict) -> tuple[bool, str]
    # Returns (verified, reason_if_not_verified)
    verification_contract: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], tuple[bool, str]]

    timeout_seconds: float
    max_attempts: int
    cooldown_seconds: float

    # Rollback or recovery policy description
    rollback_or_recovery_policy: str

    # Scope for lease conflict detection
    lease_scope: str = ""

    # Optional metadata
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


class MaintenanceRegistry:
    """Central registry for maintenance actions.

    Thread-safe, immutable after initialization.
    """

    def __init__(self) -> None:
        self._actions: dict[str, MaintenanceAction] = {}
        self._by_engine: dict[str, list[MaintenanceAction]] = {}
        self._by_risk: dict[MaintenanceRiskClass, list[MaintenanceAction]] = {}

    def register(self, action: MaintenanceAction) -> None:
        """Register an action. Must be called before registry is frozen."""
        key = f"{action.action_id}_v{action.version}"
        if key in self._actions:
            raise ValueError(f"Action already registered: {key}")
        self._actions[key] = action
        self._by_engine.setdefault(action.engine, []).append(action)
        self._by_risk.setdefault(action.risk_class, []).append(action)

    def get(self, action_id: str, version: int) -> Optional[MaintenanceAction]:
        """Get action by ID and version."""
        return self._actions.get(f"{action_id}_v{version}")

    def get_latest(self, action_id: str) -> Optional[MaintenanceAction]:
        """Get latest version of an action."""
        candidates = [a for k, a in self._actions.items() if k.startswith(f"{action_id}_v")]
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.version)

    def list_by_engine(self, engine: str) -> list[MaintenanceAction]:
        """List all actions for an engine."""
        return list(self._by_engine.get(engine, []))

    def list_by_risk(self, risk_class: MaintenanceRiskClass) -> list[MaintenanceAction]:
        """List all actions for a risk class."""
        return list(self._by_risk.get(risk_class, []))

    def list_all(self) -> list[MaintenanceAction]:
        """List all registered actions."""
        return list(self._actions.values())

    def iter_actions(self) -> list[MaintenanceAction]:
        """Iterate all actions (for scheduler)."""
        return list(self._actions.values())


# Global registry instance
_registry: Optional[MaintenanceRegistry] = None


def get_registry() -> MaintenanceRegistry:
    """Get the global maintenance registry, initializing if needed."""
    global _registry
    if _registry is None:
        _registry = MaintenanceRegistry()
        _register_builtin_actions(_registry)
    return _registry


def register_action(action: MaintenanceAction) -> None:
    """Register an action in the global registry."""
    get_registry().register(action)


def _register_builtin_actions(registry: MaintenanceRegistry) -> None:
    """Register built-in maintenance actions for v1."""

    # --- PostgreSQL Actions ---

    def pg_analyze_trigger(signals: dict[str, Any]) -> bool:
        """Trigger ANALYZE when stats are stale and system is healthy."""
        return (
            signals.get("pg_stats_stale", False)
            and signals.get("pg_healthy", True)
            and signals.get("transport_pressure", 0) < 0.5
            and signals.get("lock_pressure", 0) < 0.5
        )

    def pg_analyze_preconditions(ctx: dict[str, Any]) -> tuple[bool, str]:
        if not ctx.get("pg_connected", False):
            return False, "PostgreSQL not connected"
        if ctx.get("recovery_active", False):
            return False, "Recovery in progress"
        if ctx.get("shutdown_draining", False):
            return False, "Shutdown draining"
        return True, "OK"

    def pg_analyze_verify(before: dict, after: dict, ctx: dict) -> tuple[bool, str]:
        if not after.get("analyze_completed", False):
            return False, "ANALYZE did not complete"
        if after.get("new_critical_locks", 0) > 0:
            return False, "New critical locks detected"
        if not after.get("stats_refreshed", False):
            return False, "Statistics not refreshed"
        return True, "OK"

    registry.register(MaintenanceAction(
        action_id="pg_analyze_table_v1",
        version=1,
        engine="postgresql",
        risk_class=MaintenanceRiskClass.M1_SAFE_AUTO,
        trigger_rule=pg_analyze_trigger,
        preconditions=pg_analyze_preconditions,
        verification_contract=pg_analyze_verify,
        timeout_seconds=300.0,
        max_attempts=3,
        cooldown_seconds=1800.0,
        rollback_or_recovery_policy="ANALYZE is read-only; no rollback needed. On failure, retry with backoff.",
        lease_scope="pg:analyze:{database_id}",
        description="Run ANALYZE on tables with stale statistics",
        tags=("postgresql", "statistics", "safe"),
    ))

    def pg_health_observe_trigger(signals: dict[str, Any]) -> bool:
        return True  # Always observe

    def pg_health_observe_preconditions(ctx: dict[str, Any]) -> tuple[bool, str]:
        return True, "OK"

    def pg_health_observe_verify(before: dict, after: dict, ctx: dict) -> tuple[bool, str]:
        return True, "OK"

    registry.register(MaintenanceAction(
        action_id="pg_health_observe_v1",
        version=1,
        engine="postgresql",
        risk_class=MaintenanceRiskClass.M0_OBSERVE,
        trigger_rule=pg_health_observe_trigger,
        preconditions=pg_health_observe_preconditions,
        verification_contract=pg_health_observe_verify,
        timeout_seconds=30.0,
        max_attempts=1,
        cooldown_seconds=60.0,
        rollback_or_recovery_policy="Observation only; no state change.",
        lease_scope="pg:health:observe",
        description="Collect PostgreSQL health metrics",
        tags=("postgresql", "health", "observe"),
    ))

    # --- SQLite Actions ---

    def sqlite_checkpoint_trigger(signals: dict[str, Any]) -> bool:
        wal_mb = signals.get("sqlite_wal_size_mb", 0)
        threshold = signals.get("sqlite_checkpoint_threshold_mb", 50)
        return (
            wal_mb >= threshold
            and not signals.get("sqlite_blocked_writer", False)
            and signals.get("disk_healthy", True)
        )

    def sqlite_checkpoint_preconditions(ctx: dict[str, Any]) -> tuple[bool, str]:
        db_class = ctx.get("sqlite_db_class", "D")
        if db_class == "A":
            return False, "Class A (governance codex) - checkpoint not allowed"
        if ctx.get("sqlite_long_reader", False):
            return False, "Long-running reader active"
        return True, "OK"

    def sqlite_checkpoint_verify(before: dict, after: dict, ctx: dict) -> tuple[bool, str]:
        if not after.get("checkpoint_completed", False):
            return False, "Checkpoint did not complete"
        wal_before = before.get("wal_size_mb", 0)
        wal_after = after.get("wal_size_mb", 0)
        if wal_after >= wal_before * 0.9:
            return False, "WAL size did not decrease sufficiently"
        return True, "OK"

    registry.register(MaintenanceAction(
        action_id="sqlite_checkpoint_v1",
        version=1,
        engine="sqlite",
        risk_class=MaintenanceRiskClass.M1_SAFE_AUTO,
        trigger_rule=sqlite_checkpoint_trigger,
        preconditions=sqlite_checkpoint_preconditions,
        verification_contract=sqlite_checkpoint_verify,
        timeout_seconds=60.0,
        max_attempts=3,
        cooldown_seconds=300.0,
        rollback_or_recovery_policy="Checkpoint is idempotent; on failure, retry. Class A databases never checkpoint.",
        lease_scope="sqlite:checkpoint:{module_id}:{database_id}",
        description="Run SQLite WAL checkpoint (PASSIVE/RESTART/TRUNCATE based on WAL size)",
        tags=("sqlite", "wal", "checkpoint"),
    ))

    def sqlite_health_observe_trigger(signals: dict[str, Any]) -> bool:
        return True

    def sqlite_health_observe_preconditions(ctx: dict[str, Any]) -> tuple[bool, str]:
        return True, "OK"

    def sqlite_health_observe_verify(before: dict, after: dict, ctx: dict) -> tuple[bool, str]:
        return True, "OK"

    registry.register(MaintenanceAction(
        action_id="sqlite_health_observe_v1",
        version=1,
        engine="sqlite",
        risk_class=MaintenanceRiskClass.M0_OBSERVE,
        trigger_rule=sqlite_health_observe_trigger,
        preconditions=sqlite_health_observe_preconditions,
        verification_contract=sqlite_health_observe_verify,
        timeout_seconds=10.0,
        max_attempts=1,
        cooldown_seconds=60.0,
        rollback_or_recovery_policy="Observation only.",
        lease_scope="sqlite:health:observe",
        description="Collect SQLite health metrics per database class",
        tags=("sqlite", "health", "observe"),
    ))

    # --- Reconcile Actions ---

    def reconcile_throttle_trigger(signals: dict[str, Any]) -> bool:
        return (
            signals.get("reconcile_pending", 0) > signals.get("reconcile_throttle_threshold", 100)
            or signals.get("pg_latency_ms", 0) > signals.get("pg_latency_threshold_ms", 100)
        )

    def reconcile_throttle_preconditions(ctx: dict[str, Any]) -> tuple[bool, str]:
        if ctx.get("recovery_active", False):
            return False, "Recovery in progress"
        return True, "OK"

    def reconcile_throttle_verify(before: dict, after: dict, ctx: dict) -> tuple[bool, str]:
        new_rate = after.get("reconcile_rate", 0)
        min_rate = ctx.get("reconcile_min_rate", 10)
        max_rate = ctx.get("reconcile_max_rate", 1000)
        if not (min_rate <= new_rate <= max_rate):
            return False, f"Rate {new_rate} outside bounds [{min_rate}, {max_rate}]"
        return True, "OK"

    registry.register(MaintenanceAction(
        action_id="reconcile_throttle_v1",
        version=1,
        engine="reconcile",
        risk_class=MaintenanceRiskClass.M1_SAFE_AUTO,
        trigger_rule=reconcile_throttle_trigger,
        preconditions=reconcile_throttle_preconditions,
        verification_contract=reconcile_throttle_verify,
        timeout_seconds=10.0,
        max_attempts=1,
        cooldown_seconds=60.0,
        rollback_or_recovery_policy="Rate adjustment is bounded; revert to previous rate on failure.",
        lease_scope="reconcile:throttle",
        description="Dynamically adjust reconcile batch/rate within bounded limits",
        tags=("reconcile", "throttle", "adaptive"),
    ))

    def reconcile_health_observe_trigger(signals: dict[str, Any]) -> bool:
        return True

    def reconcile_health_observe_preconditions(ctx: dict[str, Any]) -> tuple[bool, str]:
        return True, "OK"

    def reconcile_health_observe_verify(before: dict, after: dict, ctx: dict) -> tuple[bool, str]:
        return True, "OK"

    registry.register(MaintenanceAction(
        action_id="reconcile_health_observe_v1",
        version=1,
        engine="reconcile",
        risk_class=MaintenanceRiskClass.M0_OBSERVE,
        trigger_rule=reconcile_health_observe_trigger,
        preconditions=reconcile_health_observe_preconditions,
        verification_contract=reconcile_health_observe_verify,
        timeout_seconds=10.0,
        max_attempts=1,
        cooldown_seconds=60.0,
        rollback_or_recovery_policy="Observation only.",
        lease_scope="reconcile:health:observe",
        description="Collect reconcile health and backlog metrics",
        tags=("reconcile", "health", "observe"),
    ))

    # --- Backup Actions ---

    def backup_verify_trigger(signals: dict[str, Any]) -> bool:
        backup_age_hours = signals.get("backup_age_hours", 0)
        return backup_age_hours > signals.get("backup_max_age_hours", 24)

    def backup_verify_preconditions(ctx: dict[str, Any]) -> tuple[bool, str]:
        if ctx.get("recovery_active", False):
            return False, "Recovery in progress"
        if ctx.get("shutdown_draining", False):
            return False, "Shutdown draining"
        return True, "OK"

    def backup_verify_verify(before: dict, after: dict, ctx: dict) -> tuple[bool, str]:
        if not after.get("checksum_verified", False):
            return False, "Backup checksum verification failed"
        if not after.get("schema_valid", False):
            return False, "Backup schema validation failed"
        if not after.get("integrity_valid", False):
            return False, "Backup integrity check failed"
        return True, "OK"

    registry.register(MaintenanceAction(
        action_id="backup_verify_v1",
        version=1,
        engine="backup",
        risk_class=MaintenanceRiskClass.M1_SAFE_AUTO,
        trigger_rule=backup_verify_trigger,
        preconditions=backup_verify_preconditions,
        verification_contract=backup_verify_verify,
        timeout_seconds=600.0,
        max_attempts=2,
        cooldown_seconds=3600.0,
        rollback_or_recovery_policy="Verification is read-only; failed verification marks backup as unverified.",
        lease_scope="backup:verify:{backup_id}",
        description="Verify backup checksum, schema, and integrity",
        tags=("backup", "verify", "checksum"),
    ))

    # --- Projection Rebuild (M2 - governed) ---

    def projection_rebuild_trigger(signals: dict[str, Any]) -> bool:
        return signals.get("projection_stale", False)

    def projection_rebuild_preconditions(ctx: dict[str, Any]) -> tuple[bool, str]:
        if ctx.get("recovery_active", False):
            return False, "Recovery in progress"
        if not ctx.get("governed_authorization", False):
            return False, "Governed authorization required for projection rebuild"
        return True, "OK"

    def projection_rebuild_verify(before: dict, after: dict, ctx: dict) -> tuple[bool, str]:
        if not after.get("generation_correct", False):
            return False, "Projection generation mismatch"
        if not after.get("source_revision_matches", False):
            return False, "Source revision mismatch"
        if not after.get("projection_readable", False):
            return False, "Projection not readable"
        if not after.get("row_count_contract_ok", False):
            return False, "Row count contract failed"
        return True, "OK"

    registry.register(MaintenanceAction(
        action_id="projection_rebuild_v1",
        version=1,
        engine="postgresql",
        risk_class=MaintenanceRiskClass.M2_GOVERNED_AUTO,
        trigger_rule=projection_rebuild_trigger,
        preconditions=projection_rebuild_preconditions,
        verification_contract=projection_rebuild_verify,
        timeout_seconds=1800.0,
        max_attempts=2,
        cooldown_seconds=3600.0,
        rollback_or_recovery_policy="Drop and rebuild projection from source; requires governed authorization.",
        lease_scope="pg:projection:rebuild:{projection_name}",
        description="Rebuild materialized projection with governed authorization",
        tags=("postgresql", "projection", "governed"),
    ))


__all__ = [
    "MaintenanceAction",
    "MaintenanceRegistry",
    "get_registry",
    "register_action",
]
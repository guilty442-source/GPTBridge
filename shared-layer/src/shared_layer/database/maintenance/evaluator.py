"""Maintenance Evaluator.

Converts telemetry into maintenance candidates using deterministic rules.
No LLM involvement. Each evaluator is specific to an action type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from .models import MaintenanceJob, MaintenanceRiskClass
from .registry import MaintenanceAction, get_registry


@dataclass(frozen=True)
class MaintenanceCandidate:
    """A candidate maintenance action derived from telemetry."""

    candidate_id: UUID = field(default_factory=uuid4)
    action_id: str = ""
    action_version: int = 1
    engine: str = ""
    database_id: str = ""
    module_id: str = ""
    risk_class: MaintenanceRiskClass = MaintenanceRiskClass.M0_OBSERVE
    priority: int = 0
    reason_code: str = ""
    trigger_signals: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_job(self, generation: int) -> MaintenanceJob:
        """Convert candidate to a maintenance job."""
        return MaintenanceJob(
            job_id=self.candidate_id,
            action_id=self.action_id,
            action_version=self.action_version,
            engine=self.engine,
            database_id=self.database_id,
            module_id=self.module_id,
            risk_class=self.risk_class,
            priority=self.priority,
            generation=generation,
            scheduled_at=self.created_at,
            before_state={"trigger_signals": self.trigger_signals},
        )


class Evaluator:
    """Base evaluator for maintenance candidates."""

    def __init__(self, registry: MaintenanceAction | None = None) -> None:
        self.registry = registry or get_registry()

    def evaluate(self, signals: dict[str, Any], context: dict[str, Any]) -> list[MaintenanceCandidate]:
        """Evaluate signals and produce candidates. Override in subclasses."""
        raise NotImplementedError


class PostgreSQLEvaluator(Evaluator):
    """Evaluates PostgreSQL maintenance candidates."""

    def evaluate(self, signals: dict[str, Any], context: dict[str, Any]) -> list[MaintenanceCandidate]:
        candidates = []

        # Check for ANALYZE need
        if self._should_analyze(signals, context):
            action = self.registry.get("pg_analyze_table_v1", 1)
            if action:
                candidates.append(MaintenanceCandidate(
                    action_id=action.action_id,
                    action_version=action.version,
                    engine=action.engine,
                    database_id=context.get("database_id", "primary"),
                    module_id=context.get("module_id", "maintenance"),
                    risk_class=action.risk_class,
                    priority=50,
                    reason_code="PG_ANALYZE_REQUIRED",
                    trigger_signals={
                        "stats_stale": signals.get("pg_stats_stale", False),
                        "pg_healthy": signals.get("pg_healthy", True),
                        "transport_pressure": signals.get("transport_pressure", 0),
                        "lock_pressure": signals.get("lock_pressure", 0),
                    },
                ))

        # Health observation (always runs)
        health_action = self.registry.get("pg_health_observe_v1", 1)
        if health_action:
            candidates.append(MaintenanceCandidate(
                action_id=health_action.action_id,
                action_version=health_action.version,
                engine=health_action.engine,
                database_id=context.get("database_id", "primary"),
                module_id=context.get("module_id", "maintenance"),
                risk_class=health_action.risk_class,
                priority=10,
                reason_code="HEALTH_OBSERVE",
                trigger_signals={
                    "pg_latency_ms": signals.get("pg_latency_ms", 0),
                    "pg_connections": signals.get("pg_connections", 0),
                },
            ))

        return candidates

    def _should_analyze(self, signals: dict[str, Any], context: dict[str, Any]) -> bool:
        """Determine if ANALYZE is needed."""
        return (
            signals.get("pg_stats_stale", False)
            and signals.get("pg_healthy", True)
            and signals.get("transport_pressure", 1.0) < 0.5
            and signals.get("lock_pressure", 1.0) < 0.5
            and not context.get("recovery_active", False)
            and not context.get("shutdown_draining", False)
        )


class SQLiteEvaluator(Evaluator):
    """Evaluates SQLite maintenance candidates per database class."""

    def evaluate(self, signals: dict[str, Any], context: dict[str, Any]) -> list[MaintenanceCandidate]:
        candidates = []

        # Evaluate each registered SQLite database
        sqlite_dbs = context.get("sqlite_databases", [])
        for db_info in sqlite_dbs:
            db_class = db_info.get("db_class", "D")
            module_id = db_info.get("module_id", "unknown")
            database_id = db_info.get("database_path", "unknown")

            # Health observation for all classes
            health_action = self.registry.get("sqlite_health_observe_v1", 1)
            if health_action:
                candidates.append(MaintenanceCandidate(
                    action_id=health_action.action_id,
                    action_version=health_action.version,
                    engine=health_action.engine,
                    database_id=database_id,
                    module_id=module_id,
                    risk_class=health_action.risk_class,
                    priority=10,
                    reason_code="HEALTH_OBSERVE",
                    trigger_signals={
                        "db_class": db_class,
                        "wal_size_mb": signals.get(f"sqlite_{database_id}_wal_mb", 0),
                    },
                ))

            # Class A (governance codex) - NO modifications allowed
            if db_class == "A":
                continue  # Only observation, no checkpoint/analyze

            # Checkpoint for classes B, C, D
            if self._should_checkpoint(db_class, signals, database_id, context):
                action = self.registry.get("sqlite_checkpoint_v1", 1)
                if action:
                    candidates.append(MaintenanceCandidate(
                        action_id=action.action_id,
                        action_version=action.version,
                        engine=action.engine,
                        database_id=database_id,
                        module_id=module_id,
                        risk_class=action.risk_class,
                        priority=40 if db_class == "B" else 30,
                        reason_code="SQLITE_WAL_PRESSURE",
                        trigger_signals={
                            "db_class": db_class,
                            "wal_size_mb": signals.get(f"sqlite_{database_id}_wal_mb", 0),
                            "blocked_writer": signals.get(f"sqlite_{database_id}_blocked_writer", False),
                            "long_reader": signals.get(f"sqlite_{database_id}_long_reader", False),
                            "disk_healthy": signals.get("disk_healthy", True),
                        },
                    ))

        return candidates

    def _should_checkpoint(
        self,
        db_class: str,
        signals: dict[str, Any],
        database_id: str,
        context: dict[str, Any],
    ) -> bool:
        """Determine if SQLite checkpoint is needed."""
        if db_class == "A":
            return False  # Never checkpoint governance codex

        wal_mb = signals.get(f"sqlite_{database_id}_wal_mb", 0)
        threshold = signals.get("sqlite_checkpoint_threshold_mb", 50)

        if wal_mb < threshold:
            return False

        if signals.get(f"sqlite_{database_id}_blocked_writer", False):
            return False

        if signals.get(f"sqlite_{database_id}_long_reader", False):
            # Policy decision: some classes allow checkpoint with long readers
            if db_class in ("C", "D"):
                pass  # Allow for runtime/cache
            else:
                return False

        if not signals.get("disk_healthy", True):
            return False

        if context.get("recovery_active", False) or context.get("shutdown_draining", False):
            return False

        return True


class ReconcileEvaluator(Evaluator):
    """Evaluates reconcile maintenance candidates."""

    def evaluate(self, signals: dict[str, Any], context: dict[str, Any]) -> list[MaintenanceCandidate]:
        candidates = []

        # Throttle adjustment
        if self._should_throttle(signals, context):
            action = self.registry.get("reconcile_throttle_v1", 1)
            if action:
                # Determine direction
                pg_latency = signals.get("pg_latency_ms", 0)
                transport_backlog = signals.get("transport_backlog", 0)
                pending = signals.get("reconcile_pending", 0)

                if pg_latency > signals.get("pg_latency_threshold_ms", 100) or transport_backlog > 1000:
                    reason = "RECONCILE_THROTTLED"
                else:
                    reason = "RECONCILE_BACKLOG_HIGH"

                candidates.append(MaintenanceCandidate(
                    action_id=action.action_id,
                    action_version=action.version,
                    engine=action.engine,
                    database_id="primary",
                    module_id="reconcile",
                    risk_class=action.risk_class,
                    priority=20,
                    reason_code=reason,
                    trigger_signals={
                        "pg_latency_ms": pg_latency,
                        "transport_backlog": transport_backlog,
                        "reconcile_pending": pending,
                        "current_rate": signals.get("reconcile_rate", 0),
                    },
                ))

        # Health observation
        health_action = self.registry.get("reconcile_health_observe_v1", 1)
        if health_action:
            candidates.append(MaintenanceCandidate(
                action_id=health_action.action_id,
                action_version=health_action.version,
                engine=health_action.engine,
                database_id="primary",
                module_id="reconcile",
                risk_class=health_action.risk_class,
                priority=10,
                reason_code="HEALTH_OBSERVE",
                trigger_signals={
                    "reconcile_pending": signals.get("reconcile_pending", 0),
                    "reconcile_rate": signals.get("reconcile_rate", 0),
                },
            ))

        return candidates

    def _should_throttle(self, signals: dict[str, Any], context: dict[str, Any]) -> bool:
        """Determine if reconcile throttle adjustment is needed."""
        if context.get("recovery_active", False):
            return False

        pending = signals.get("reconcile_pending", 0)
        threshold = signals.get("reconcile_throttle_threshold", 100)
        pg_latency = signals.get("pg_latency_ms", 0)
        latency_threshold = signals.get("pg_latency_threshold_ms", 100)

        return (
            pending > threshold
            or pg_latency > latency_threshold
        )


class BackupEvaluator(Evaluator):
    """Evaluates backup maintenance candidates."""

    def evaluate(self, signals: dict[str, Any], context: dict[str, Any]) -> list[MaintenanceCandidate]:
        candidates = []

        # Backup verification
        if self._should_verify_backup(signals, context):
            action = self.registry.get("backup_verify_v1", 1)
            if action:
                candidates.append(MaintenanceCandidate(
                    action_id=action.action_id,
                    action_version=action.version,
                    engine=action.engine,
                    database_id=context.get("backup_id", "latest"),
                    module_id="backup",
                    risk_class=action.risk_class,
                    priority=30,
                    reason_code="BACKUP_STALE",
                    trigger_signals={
                        "backup_age_hours": signals.get("backup_age_hours", 0),
                        "max_age_hours": signals.get("backup_max_age_hours", 24),
                    },
                ))

        return candidates

    def _should_verify_backup(self, signals: dict[str, Any], context: dict[str, Any]) -> bool:
        """Determine if backup verification is needed."""
        if context.get("recovery_active", False) or context.get("shutdown_draining", False):
            return False

        backup_age = signals.get("backup_age_hours", 0)
        max_age = signals.get("backup_max_age_hours", 24)

        return backup_age > max_age


def evaluate_candidates(
    signals: dict[str, Any],
    context: dict[str, Any],
) -> list[MaintenanceCandidate]:
    """Run all evaluators and collect candidates."""
    all_candidates = []

    # PostgreSQL evaluator
    pg_eval = PostgreSQLEvaluator()
    all_candidates.extend(pg_eval.evaluate(signals, context))

    # SQLite evaluator
    sqlite_eval = SQLiteEvaluator()
    all_candidates.extend(sqlite_eval.evaluate(signals, context))

    # Reconcile evaluator
    reconcile_eval = ReconcileEvaluator()
    all_candidates.extend(reconcile_eval.evaluate(signals, context))

    # Backup evaluator
    backup_eval = BackupEvaluator()
    all_candidates.extend(backup_eval.evaluate(signals, context))

    # Sort by priority (lower = higher priority)
    all_candidates.sort(key=lambda c: c.priority)

    return all_candidates


__all__ = [
    "MaintenanceCandidate",
    "Evaluator",
    "PostgreSQLEvaluator",
    "SQLiteEvaluator",
    "ReconcileEvaluator",
    "BackupEvaluator",
    "evaluate_candidates",
]
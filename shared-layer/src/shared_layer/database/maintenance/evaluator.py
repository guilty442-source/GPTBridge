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



def evaluate_candidates(
    signals: dict[str, Any],
    context: dict[str, Any],
) -> list[MaintenanceCandidate]:
    """Run all evaluators and collect candidates."""
    all_candidates = []

    # PostgreSQL evaluator
    pg_eval = PostgreSQLEvaluator()
    all_candidates.extend(pg_eval.evaluate(signals, context))

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
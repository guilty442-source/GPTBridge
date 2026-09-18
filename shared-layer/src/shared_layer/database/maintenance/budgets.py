"""Maintenance Budget.

Bounded resource budgets for maintenance operations.
Maintenance priority is always lower than:
1. Recovery
2. Critical transport
3. Authority write
4. Reconcile critical backlog
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class BudgetConfig:
    """Configuration for maintenance budgets."""

    # Concurrency limits
    max_concurrent_jobs: int = 2
    pg_maintenance_connection_limit: int = 2
    sqlite_simultaneous_checkpoint_limit: int = 3
    backup_verification_concurrency: int = 1

    # Reconcile limits
    reconcile_max_rows_per_second: int = 5000
    reconcile_max_batch_size: int = 500
    reconcile_min_batch_size: int = 10
    reconcile_max_workers: int = 2

    # Runtime limits
    maintenance_max_runtime_seconds: float = 1800.0  # 30 minutes
    default_cooldown_seconds: float = 300.0

    # Priority: maintenance must yield to these (lower number = higher priority)
    priority_recovery: int = 0
    priority_critical_transport: int = 10
    priority_authority_write: int = 20
    priority_reconcile_critical: int = 30
    priority_maintenance: int = 100


@dataclass
class BudgetState:
    """Current budget utilization state."""

    active_jobs: int = 0
    pg_maintenance_connections: int = 0
    sqlite_checkpoints_active: int = 0
    backup_verifications_active: int = 0
    reconcile_current_rate: int = 0
    reconcile_current_batch: int = 0
    reconcile_workers_active: int = 0

    # Track per-engine usage
    engine_usage: dict[str, int] = field(default_factory=dict)

    # Cooldown tracking
    last_maintenance_time: dict[str, datetime] = field(default_factory=dict)


class MaintenanceBudget:
    """Thread-safe budget enforcement for maintenance operations."""

    def __init__(self, config: BudgetConfig | None = None) -> None:
        self.config = config or BudgetConfig()
        self._state = BudgetState()
        self._lock = RLock()

    def can_start_job(self, engine: str, risk_class: str) -> tuple[bool, str]:
        """Check if a new maintenance job can start."""
        with self._lock:
            if self._state.active_jobs >= self.config.max_concurrent_jobs:
                return False, f"Max concurrent jobs ({self.config.max_concurrent_jobs}) reached"

            # Engine-specific limits
            if engine == "postgresql":
                if self._state.pg_maintenance_connections >= self.config.pg_maintenance_connection_limit:
                    return False, f"PG maintenance connection limit ({self.config.pg_maintenance_connection_limit}) reached"
            elif engine == "sqlite":
                if self._state.sqlite_checkpoints_active >= self.config.sqlite_simultaneous_checkpoint_limit:
                    return False, f"SQLite checkpoint limit ({self.config.sqlite_simultaneous_checkpoint_limit}) reached"
            elif engine == "backup":
                if self._state.backup_verifications_active >= self.config.backup_verification_concurrency:
                    return False, f"Backup verification limit ({self.config.backup_verification_concurrency}) reached"

            return True, "OK"

    def reserve_job(self, engine: str) -> None:
        """Reserve budget for a starting job."""
        with self._lock:
            self._state.active_jobs += 1
            self._state.engine_usage[engine] = self._state.engine_usage.get(engine, 0) + 1

            if engine == "postgresql":
                self._state.pg_maintenance_connections += 1
            elif engine == "sqlite":
                self._state.sqlite_checkpoints_active += 1
            elif engine == "backup":
                self._state.backup_verifications_active += 1

    def release_job(self, engine: str) -> None:
        """Release budget after job completes."""
        with self._lock:
            self._state.active_jobs = max(0, self._state.active_jobs - 1)
            self._state.engine_usage[engine] = max(0, self._state.engine_usage.get(engine, 0) - 1)

            if engine == "postgresql":
                self._state.pg_maintenance_connections = max(0, self._state.pg_maintenance_connections - 1)
            elif engine == "sqlite":
                self._state.sqlite_checkpoints_active = max(0, self._state.sqlite_checkpoints_active - 1)
            elif engine == "backup":
                self._state.backup_verifications_active = max(0, self._state.backup_verifications_active - 1)

    def can_adjust_reconcile_rate(self, new_rate: int, new_batch: int) -> tuple[bool, str]:
        """Check if reconcile rate/batch adjustment is within bounds."""
        with self._lock:
            if not (self.config.reconcile_min_batch_size <= new_batch <= self.config.reconcile_max_batch_size):
                return False, f"Batch size {new_batch} outside bounds [{self.config.reconcile_min_batch_size}, {self.config.reconcile_max_batch_size}]"

            max_rate = self.config.reconcile_max_rows_per_second
            if not (0 <= new_rate <= max_rate):
                return False, f"Rate {new_rate} outside bounds [0, {max_rate}]"

            return True, "OK"

    def reserve_reconcile_adjustment(self, new_rate: int, new_batch: int) -> None:
        """Reserve budget for reconcile rate adjustment."""
        with self._lock:
            self._state.reconcile_current_rate = new_rate
            self._state.reconcile_current_batch = new_batch

    def release_reconcile_adjustment(self) -> None:
        """Release reconcile adjustment reservation."""
        with self._lock:
            self._state.reconcile_current_rate = 0
            self._state.reconcile_current_batch = 0

    def check_cooldown(self, action_id: str, cooldown_seconds: float) -> tuple[bool, str]:
        """Check if action is in cooldown."""
        with self._lock:
            last_time = self._state.last_maintenance_time.get(action_id)
            if last_time is None:
                return True, "OK"

            elapsed = (datetime.utcnow() - last_time).total_seconds()
            if elapsed < cooldown_seconds:
                return False, f"Cooldown active: {cooldown_seconds - elapsed:.0f}s remaining"
            return True, "OK"

    def record_maintenance(self, action_id: str) -> None:
        """Record maintenance execution time."""
        with self._lock:
            self._state.last_maintenance_time[action_id] = datetime.utcnow()

    def get_priority(self) -> int:
        """Get maintenance priority level (higher = lower priority)."""
        return self.config.priority_maintenance

    def snapshot(self) -> dict[str, Any]:
        """Get current budget state snapshot."""
        with self._lock:
            return {
                "active_jobs": self._state.active_jobs,
                "max_concurrent_jobs": self.config.max_concurrent_jobs,
                "pg_maintenance_connections": self._state.pg_maintenance_connections,
                "pg_maintenance_connection_limit": self.config.pg_maintenance_connection_limit,
                "sqlite_checkpoints_active": self._state.sqlite_checkpoints_active,
                "sqlite_simultaneous_checkpoint_limit": self.config.sqlite_simultaneous_checkpoint_limit,
                "backup_verifications_active": self._state.backup_verifications_active,
                "backup_verification_concurrency": self.config.backup_verification_concurrency,
                "reconcile_current_rate": self._state.reconcile_current_rate,
                "reconcile_max_rate": self.config.reconcile_max_rows_per_second,
                "reconcile_current_batch": self._state.reconcile_current_batch,
                "reconcile_batch_bounds": [
                    self.config.reconcile_min_batch_size,
                    self.config.reconcile_max_batch_size,
                ],
                "maintenance_priority": self.config.priority_maintenance,
                "engine_usage": dict(self._state.engine_usage),
            }


DEFAULT_BUDGET_CONFIG = BudgetConfig()


def check_budget(
    budget: MaintenanceBudget,
    engine: str,
    risk_class: str,
) -> tuple[bool, str]:
    """Convenience function to check budget."""
    return budget.can_start_job(engine, risk_class)


__all__ = [
    "BudgetConfig",
    "BudgetState",
    "MaintenanceBudget",
    "DEFAULT_BUDGET_CONFIG",
    "check_budget",
]
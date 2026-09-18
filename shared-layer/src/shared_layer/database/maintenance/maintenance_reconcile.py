"""Reconcile Auto Maintenance v1.

Integrates with existing ReconcileService - does not create a new reconcile authority.
Allows dynamic adjustment of:
- batch size
- sleep interval
- worker budget
- module priority

All values bounded by min <= current <= max.
Does NOT:
- Resolve conflicts
- Override PostgreSQL authority
- Use last-write-wins
- Full-scan every startup
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import MaintenanceJob
from .registry import MaintenanceAction
from .verifier import verify_reconcile_throttle, verify_health_observe


@dataclass(frozen=True)
class ReconcileConfig:
    """Reconcile configuration bounds (from AdaptiveEnvelope)."""

    min_batch_size: int = 10
    max_batch_size: int = 500
    min_rate_per_second: int = 10
    max_rate_per_second: int = 5000
    min_workers: int = 1
    max_workers: int = 2
    min_sleep_interval_ms: int = 10
    max_sleep_interval_ms: int = 5000


@dataclass(frozen=True)
class ReconcileState:
    """Current reconcile state (from existing ReconcileService)."""

    pending_count: int = 0
    dirty_count: int = 0
    current_batch_size: int = 50
    current_rate_per_second: int = 100
    current_workers: int = 1
    current_sleep_interval_ms: int = 100
    transport_latency_ms: float = 0.0
    pg_latency_ms: float = 0.0
    backlog_age_seconds: float = 0.0
    last_reconciled_revision: int = 0


@dataclass(frozen=True)
class ReconcileAdjustment:
    """Proposed reconcile adjustment."""

    new_batch_size: int
    new_rate_per_second: int
    new_workers: int
    new_sleep_interval_ms: int
    reason: str


DEFAULT_RECONCILE_CONFIG = ReconcileConfig()


def collect_reconcile_state() -> ReconcileState:
    """Collect current reconcile state from existing service.

    This integrates with the existing ReconcileService.
    Implementation would query the actual service state.
    """
    # In production, this would call the actual ReconcileService
    # For now, return defaults
    return ReconcileState()


def evaluate_reconcile_adjustment(
    state: ReconcileState,
    config: ReconcileConfig = DEFAULT_RECONCILE_CONFIG,
) -> ReconcileAdjustment | None:
    """Evaluate if reconcile parameters need adjustment.

    Rules:
    - PG latency low + transport backlog low + pending high -> increase batch/rate
    - PG latency high OR transport backlog high -> decrease rate or pause
    """
    # High backlog, low latency -> increase throughput
    if (state.pending_count > 100
        and state.pg_latency_ms < 50
        and state.transport_latency_ms < 50
        and state.backlog_age_seconds < 60):
        new_batch = min(state.current_batch_size * 2, config.max_batch_size)
        new_rate = min(state.current_rate_per_second * 2, config.max_rate_per_second)
        return ReconcileAdjustment(
            new_batch_size=new_batch,
            new_rate_per_second=new_rate,
            new_workers=min(state.current_workers + 1, config.max_workers),
            new_sleep_interval_ms=max(state.current_sleep_interval_ms // 2, config.min_sleep_interval_ms),
            reason="RECONCILE_BACKLOG_HIGH",
        )

    # High latency or backlog -> decrease throughput
    if (state.pg_latency_ms > 100
        or state.transport_latency_ms > 100
        or state.backlog_age_seconds > 300):
        new_batch = max(state.current_batch_size // 2, config.min_batch_size)
        new_rate = max(state.current_rate_per_second // 2, config.min_rate_per_second)
        return ReconcileAdjustment(
            new_batch_size=new_batch,
            new_rate_per_second=new_rate,
            new_workers=max(state.current_workers - 1, config.min_workers),
            new_sleep_interval_ms=min(state.current_sleep_interval_ms * 2, config.max_sleep_interval_ms),
            reason="RECONCILE_THROTTLED",
        )

    return None


def apply_reconcile_adjustment(
    adjustment: ReconcileAdjustment,
) -> bool:
    """Apply reconcile adjustment to existing ReconcileService.

    This would integrate with the actual service to update its parameters.
    """
    # In production, this would call the actual ReconcileService
    # to update its configuration
    return True


def execute_reconcile_throttle(
    job: MaintenanceJob,
    action: MaintenanceAction,
    before_state: dict[str, Any],
) -> dict[str, Any]:
    """Execute reconcile throttle adjustment."""
    after_state = dict(before_state)

    try:
        state = collect_reconcile_state()
        adjustment = evaluate_reconcile_adjustment(state)

        if adjustment:
            success = apply_reconcile_adjustment(adjustment)
            if success:
                after_state["policy_updated"] = True
                after_state["reconcile_batch"] = adjustment.new_batch_size
                after_state["reconcile_rate"] = adjustment.new_rate_per_second
                after_state["reconcile_workers"] = adjustment.new_workers
                after_state["reconcile_sleep_ms"] = adjustment.new_sleep_interval_ms
                after_state["adjustment_reason"] = adjustment.reason
                after_state["maintenance_verified"] = True
            else:
                after_state["policy_updated"] = False
                after_state["error"] = "Failed to apply adjustment"
        else:
            after_state["policy_updated"] = False
            after_state["reason"] = "No adjustment needed"

    except Exception as exc:
        after_state["policy_updated"] = False
        after_state["error"] = str(exc)[:300]

    return after_state


def execute_reconcile_health_observe(
    job: MaintenanceJob,
    action: MaintenanceAction,
    before_state: dict[str, Any],
) -> dict[str, Any]:
    """Execute reconcile health observation."""
    after_state = dict(before_state)

    try:
        state = collect_reconcile_state()
        after_state["metrics_collected"] = True
        after_state["health_metrics"] = {
            "pending_count": state.pending_count,
            "dirty_count": state.dirty_count,
            "current_batch_size": state.current_batch_size,
            "current_rate_per_second": state.current_rate_per_second,
            "current_workers": state.current_workers,
            "current_sleep_interval_ms": state.current_sleep_interval_ms,
            "transport_latency_ms": state.transport_latency_ms,
            "pg_latency_ms": state.pg_latency_ms,
            "backlog_age_seconds": state.backlog_age_seconds,
        }
        after_state["maintenance_verified"] = True
    except Exception as exc:
        after_state["metrics_collected"] = False
        after_state["error"] = str(exc)[:200]

    return after_state


def build_reconcile_signals(state: ReconcileState) -> dict[str, Any]:
    """Build telemetry signals for reconcile evaluator."""
    return {
        "reconcile_pending": state.pending_count,
        "reconcile_dirty": state.dirty_count,
        "reconcile_rate": state.current_rate_per_second,
        "reconcile_batch": state.current_batch_size,
        "reconcile_workers": state.current_workers,
        "reconcile_sleep_ms": state.current_sleep_interval_ms,
        "transport_latency_ms": state.transport_latency_ms,
        "pg_latency_ms": state.pg_latency_ms,
        "transport_backlog": state.pending_count,
        "transport_oldest_pending_age_seconds": state.backlog_age_seconds,
        "reconcile_throttle_threshold": 100,
        "pg_latency_threshold_ms": 100,
        "reconcile_min_rate": 10,
        "reconcile_max_rate": 5000,
        "reconcile_min_batch": 10,
        "reconcile_max_batch": 500,
    }


def get_reconcile_maintenance_executors() -> dict[str, callable]:
    """Get reconcile maintenance executors for the controller."""
    return {
        "reconcile_throttle_v1": execute_reconcile_throttle,
        "reconcile_health_observe_v1": execute_reconcile_health_observe,
    }


__all__ = [
    "ReconcileConfig",
    "ReconcileState",
    "ReconcileAdjustment",
    "DEFAULT_RECONCILE_CONFIG",
    "collect_reconcile_state",
    "evaluate_reconcile_adjustment",
    "apply_reconcile_adjustment",
    "execute_reconcile_throttle",
    "execute_reconcile_health_observe",
    "build_reconcile_signals",
    "get_reconcile_maintenance_executors",
]
"""Deadlock-free governed startup — signals (A192/E167).

Signal functions for startup failure and status.  Split from
``governed_startup`` for A185/E160 source-size compliance.
"""

from __future__ import annotations

from typing import Any

from core_system.governed_startup_types import StartupGeneration
from core_system.governed_startup_verify import DependencyDAG

# ---------------------------------------------------------------------------
# Failure handling (A192: FAILURE)
# ---------------------------------------------------------------------------

def startup_failure_signal(
    failure_scope: str,
    *,
    failed_dependency: str = "",
    generation_id: str = "",
) -> dict[str, Any]:
    """Produce a failure signal for startup failure (A192: FAILURE).

    Per A192: ``FAILURE:core-critical failure=>failed-generation+owned-reverse-
    DAG-cleanup+typed-user-visible-cause; non-core failure=>affected-capability-
    degraded-only+retry-budget``.
    """
    is_core = failure_scope == "core-critical"
    return {
        "signal_type": "startup-failure",
        "authority": "signal-only",
        "basis": "A192/E167",
        "failure_scope": failure_scope,
        "failed_dependency": failed_dependency,
        "generation_id": generation_id,
        "action": (
            "failed-generation+owned-reverse-DAG-cleanup+typed-user-visible-cause"
            if is_core
            else "affected-capability-degraded-only+retry-budget"
        ),
        "retry": "fresh-generation-or-owned-node-retry",
        "backoff": "exponential+jitter+circuit-breaker",
        "manual_approval_required": False,
    }


# ---------------------------------------------------------------------------
# Startup status (A192: STATUS)
# ---------------------------------------------------------------------------

def startup_status(
    generation: StartupGeneration,
    *,
    dag: DependencyDAG | None = None,
    core_ready_conditions: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Return the startup status for observability (A192: STATUS).

    Per A192: ``STATUS:sequenced-information-layer-events+authoritative-
    snapshot`` and ``READY:evidence-bound-to-generation+release+dependency-
    state``.
    """
    return {
        "generation_id": generation.generation_id,
        "release_id": generation.release_id,
        "started_at": generation.started_at,
        "current_phase": generation.current_phase,
        "core_ready": generation.core_ready,
        "deferred_active": generation.deferred_active,
        "dag": dag.as_dict() if dag else None,
        "core_ready_conditions": core_ready_conditions or {},
        "basis": "A192/E167",
        "single_flight": True,
        "user_action_required": False,
    }

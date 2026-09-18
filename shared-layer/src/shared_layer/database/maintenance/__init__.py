"""Database Auto Maintenance v1.

Governed, observable, interruptible, verifiable, low-risk local database
automatic maintenance control plane.

PostgreSQL is the central structured authority.
SQLite handles governance codex, module-private state, checkpoint, cache,
bounded fallback.
Qdrant is the canonical semantic/vector index.

Maintenance actions are registered, policy-gated, budget-constrained,
lease-protected, and verified after execution.
"""

from __future__ import annotations

from .models import (
    MaintenanceRiskClass,
    MaintenanceJobStatus,
    MaintenanceJob,
)
from .registry import (
    MaintenanceAction,
    MaintenanceRegistry,
    get_registry,
    register_action,
)
from .policies import (
    MaintenancePolicy,
    MaintenanceDecision,
    evaluate_policy,
)
from .budgets import (
    MaintenanceBudget,
    BudgetConfig,
    BudgetState,
    check_budget,
)
from .leases import (
    MaintenanceLease,
    LeaseManager,
    acquire_lease,
    renew_lease,
    release_lease,
    recover_expired_leases,
)
from .evaluator import (
    MaintenanceCandidate,
    Evaluator,
    evaluate_candidates,
)
from .scheduler import (
    MaintenanceScheduler,
    SchedulerConfig,
    schedule_candidates,
)
from .controller import (
    MaintenanceController,
    ControllerConfig,
    run_maintenance_cycle,
)
from .verifier import (
    VerificationResult,
    Verifier,
    verify_action,
)

__all__ = [
    "MaintenanceRiskClass",
    "MaintenanceJobStatus",
    "MaintenanceJob",
    "MaintenanceAction",
    "MaintenanceRegistry",
    "get_registry",
    "register_action",
    "MaintenancePolicy",
    "MaintenanceDecision",
    "evaluate_policy",
    "MaintenanceBudget",
    "BudgetConfig",
    "BudgetState",
    "check_budget",
    "MaintenanceLease",
    "LeaseManager",
    "acquire_lease",
    "renew_lease",
    "release_lease",
    "recover_expired_leases",
    "MaintenanceCandidate",
    "Evaluator",
    "evaluate_candidates",
    "MaintenanceScheduler",
    "SchedulerConfig",
    "schedule_candidates",
    "MaintenanceController",
    "ControllerConfig",
    "run_maintenance_cycle",
    "VerificationResult",
    "Verifier",
    "verify_action",
]

# Engine-specific modules (not exported by default to avoid circular imports)
# from . import maintenance_postgres
# from . import maintenance_sqlite
# from . import maintenance_reconcile
# from . import maintenance_backup
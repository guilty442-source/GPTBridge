"""Binding Package — A367 Binding Dependency Order.

A367: The binding dependency order is exactly:
1. RECONCILIATION (PostgreSQL<->SQLite authority/revision/hash/status)
2. GLOBAL_ID_VERSION (one resource_id and version across all modules)
"""

from .order import (
    BindingPhase,
    BindingOrderResult,
    BindingOrderEnforcer,
    create_binding_order_enforcer,
)

from .reconciliation import (
    ReconcileResult,
    ReconcileService,
)

from .global_id_version import (
    GlobalIdVersionService,
)

__all__ = [
    # Order enforcement
    "BindingPhase",
    "BindingOrderResult",
    "BindingOrderEnforcer",
    "create_binding_order_enforcer",
    # Phase 1: Reconciliation
    "ReconcileResult",
    "ReconcileService",
    # Phase 2: Global ID Version
    "GlobalIdVersionService",
]
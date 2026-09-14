"""Pool Isolation Package — A368 POOL-ISOLATION.

A368: POOL-ISOLATION: central-index, transport, audit and each module-private database
use separately budgeted connection pools with owner,max/open/idle/wait limits.
"""

from .manager import (
    PoolOwner,
    PoolBudget,
    PoolStats,
    IsolatedPool,
    PoolIsolationManager,
    DEFAULT_POOL_BUDGETS,
    create_pool_manager_from_env,
)

__all__ = [
    "PoolOwner",
    "PoolBudget",
    "PoolStats",
    "IsolatedPool",
    "PoolIsolationManager",
    "DEFAULT_POOL_BUDGETS",
    "create_pool_manager_from_env",
]
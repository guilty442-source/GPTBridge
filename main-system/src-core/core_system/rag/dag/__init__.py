"""RAG DAG package — A549 DAG orchestration plane."""

from __future__ import annotations

from .contracts import (
    REGISTERED_NODE_CATALOG,
    TERMINAL_STATES,
    RagDagBudgets,
    RagDagEdge,
    RagDagExecutionContext,
    RagDagExecutionResult,
    RagDagKind,
    RagDagNode,
    RagDagNodeResult,
    RagDagNodeSpec,
    RagDagNodeType,
    RagDagPlan,
    RagDagState,
    node_spec,
)
from .executor import (
    DEFAULT_NODE_TIMEOUT_SECONDS,
    RagDagExecutionError,
    RagDagExecutor,
)
from .planner import (
    HARD_MAX_COST,
    HARD_MAX_ROUNDS,
    HARD_MAX_SECONDS,
    HARD_MAX_STEPS,
    KIND_ORDERING,
    KIND_REQUIRED_NODE_TYPES,
    RagDagPlanRejected,
    RagDagPlanRequest,
    RagDagPlanner,
    default_budgets,
    execution_context,
)

__all__ = [
    "DEFAULT_NODE_TIMEOUT_SECONDS",
    "HARD_MAX_COST",
    "HARD_MAX_ROUNDS",
    "HARD_MAX_SECONDS",
    "HARD_MAX_STEPS",
    "KIND_ORDERING",
    "KIND_REQUIRED_NODE_TYPES",
    "REGISTERED_NODE_CATALOG",
    "TERMINAL_STATES",
    "RagDagBudgets",
    "RagDagEdge",
    "RagDagExecutionContext",
    "RagDagExecutionError",
    "RagDagExecutionResult",
    "RagDagExecutor",
    "RagDagKind",
    "RagDagNode",
    "RagDagNodeResult",
    "RagDagNodeSpec",
    "RagDagNodeType",
    "RagDagPlan",
    "RagDagPlanRejected",
    "RagDagPlanRequest",
    "RagDagPlanner",
    "RagDagState",
    "default_budgets",
    "execution_context",
    "node_spec",
]

"""RAG DAG contracts — A549 DAG orchestration plane.

A549 declares the project-wide DAG/CAG/RAG hybrid architecture.  This module
is the DAG contract surface: the registered node catalog, the closed state
set, the declarative node/edge records and the execution context/result
records.  The planner and executor build on these types and fail closed on
cycles, unregistered nodes, unauthorized dependencies and incomplete
evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RagDagNodeType(str, Enum):
    """Registered DAG node types (A549 fixed catalog)."""

    RETRIEVAL = "retrieval"
    CACHE_LOOKUP = "cache-lookup"
    CACHE_VALIDATE = "cache-validate"
    FUSION = "fusion"
    RERANK = "rerank"
    CONTEXT_BUILD = "context-build"
    MODEL_INFERENCE = "model-inference"
    CITATION_VALIDATION = "citation-validation"
    INDEX = "index"
    REPAIR = "repair"
    VERIFICATION = "verification"
    PUBLISH_BARRIER = "publish-barrier"


class RagDagState(str, Enum):
    """Closed node/execution state set (A549)."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    REQUIRES_RECONCILE = "requires-reconcile"
    QUARANTINED = "quarantined"


class RagDagKind(str, Enum):
    """Bounded DAG kinds (A549).

    ``RETRIEVAL_CHAIN`` covers the retrieval-plane chain
    (retrieval → fusion → rerank → context-build).  It deliberately
    ends before MODEL_INFERENCE/CITATION_VALIDATION: the retrieval
    layer produces evidence + context, generation happens downstream —
    a retrieval chain must never fabricate ``answer_text`` evidence."""

    QUERY = "query"
    MULTI_RAG = "multi-rag"
    RETRIEVAL_CHAIN = "retrieval-chain"
    INDEX = "index"
    REBUILD = "rebuild"
    REPAIR = "repair"


TERMINAL_STATES: frozenset[RagDagState] = frozenset(
    {
        RagDagState.SUCCEEDED,
        RagDagState.FAILED,
        RagDagState.SKIPPED,
        RagDagState.CANCELLED,
        RagDagState.BLOCKED,
        RagDagState.REQUIRES_RECONCILE,
        RagDagState.QUARANTINED,
    }
)


@dataclass(frozen=True, slots=True)
class RagDagNodeSpec:
    """Declarative catalog entry for one registered node type."""

    node_type: RagDagNodeType
    required_inputs: tuple[str, ...]
    required_evidence: tuple[str, ...]
    side_effecting: bool = False
    permission_scope_required: bool = False


REGISTERED_NODE_CATALOG: Mapping[RagDagNodeType, RagDagNodeSpec] = {
    RagDagNodeType.RETRIEVAL: RagDagNodeSpec(
        RagDagNodeType.RETRIEVAL,
        required_inputs=("query", "module_ids", "scope"),
        required_evidence=("candidates", "provenance"),
    ),
    RagDagNodeType.CACHE_LOOKUP: RagDagNodeSpec(
        RagDagNodeType.CACHE_LOOKUP,
        required_inputs=("cache_key",),
        required_evidence=("cache_hit", "cache_metadata"),
    ),
    RagDagNodeType.CACHE_VALIDATE: RagDagNodeSpec(
        RagDagNodeType.CACHE_VALIDATE,
        required_inputs=("cache_entry",),
        required_evidence=(
            "scope_check",
            "permission_check",
            "revision_check",
            "expiry_check",
            "authority_check",
        ),
    ),
    RagDagNodeType.FUSION: RagDagNodeSpec(
        RagDagNodeType.FUSION,
        required_inputs=("candidate_sets",),
        required_evidence=("fused_candidates", "fusion_method"),
    ),
    RagDagNodeType.RERANK: RagDagNodeSpec(
        RagDagNodeType.RERANK,
        required_inputs=("fused_candidates", "reranker_limit"),
        required_evidence=("reranked_candidates", "reranker_model"),
    ),
    RagDagNodeType.CONTEXT_BUILD: RagDagNodeSpec(
        RagDagNodeType.CONTEXT_BUILD,
        required_inputs=("reranked_candidates", "max_context_tokens"),
        required_evidence=("context_text", "token_budget"),
    ),
    RagDagNodeType.MODEL_INFERENCE: RagDagNodeSpec(
        RagDagNodeType.MODEL_INFERENCE,
        required_inputs=("context_text", "model_identity"),
        required_evidence=("answer_text", "model_identity", "policy_version"),
    ),
    RagDagNodeType.CITATION_VALIDATION: RagDagNodeSpec(
        RagDagNodeType.CITATION_VALIDATION,
        required_inputs=("answer_text", "citations"),
        required_evidence=("validated_citations", "citation_verdict"),
    ),
    RagDagNodeType.INDEX: RagDagNodeSpec(
        RagDagNodeType.INDEX,
        required_inputs=("resource_id", "content_hash", "generation_id"),
        required_evidence=("indexed_points", "source_revision"),
        side_effecting=True,
        permission_scope_required=True,
    ),
    RagDagNodeType.REPAIR: RagDagNodeSpec(
        RagDagNodeType.REPAIR,
        required_inputs=("repair_target", "repair_plan"),
        required_evidence=("repair_result", "rollback_reference"),
        side_effecting=True,
        permission_scope_required=True,
    ),
    RagDagNodeType.VERIFICATION: RagDagNodeSpec(
        RagDagNodeType.VERIFICATION,
        required_inputs=("verification_target",),
        required_evidence=("verification_result", "independent_verifier"),
    ),
    RagDagNodeType.PUBLISH_BARRIER: RagDagNodeSpec(
        RagDagNodeType.PUBLISH_BARRIER,
        required_inputs=("publish_target", "verification_result"),
        required_evidence=("publish_state", "authority_marker"),
        side_effecting=True,
        permission_scope_required=True,
    ),
}


def node_spec(node_type: RagDagNodeType) -> RagDagNodeSpec:
    """Return the registered catalog entry, failing closed for unknown types."""
    spec = REGISTERED_NODE_CATALOG.get(node_type)
    if spec is None:
        raise KeyError(f"unregistered-node-type:{node_type}")
    return spec


@dataclass(frozen=True, slots=True)
class RagDagEdge:
    """One declared, authorized dependency between two nodes."""

    source_id: str
    target_id: str
    kind: str = "data"
    authorized: bool = True

    def to_record(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "kind": self.kind,
            "authorized": self.authorized,
        }


@dataclass(frozen=True, slots=True)
class RagDagNode:
    """One declarative DAG node."""

    node_id: str
    node_type: RagDagNodeType
    inputs: Mapping[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    state: RagDagState = RagDagState.PENDING
    attempts: int = 0
    evidence: Mapping[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    completed_at: float | None = None
    duration_ms: int = 0
    input_contract: str = ""
    output_contract: str = ""
    failure_policy: str = "FAIL_CLOSED"

    def to_record(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type.value,
            "inputs": dict(self.inputs),
            "dependencies": list(self.dependencies),
            "state": self.state.value,
            "attempts": self.attempts,
            "evidence": dict(self.evidence),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "input_contract": self.input_contract,
            "output_contract": self.output_contract,
            "failure_policy": self.failure_policy,
        }


@dataclass(frozen=True, slots=True)
class RagDagBudgets:
    """Hard bounds every DAG carries (A549 agentic extension limits)."""

    max_steps: int
    max_seconds: float
    max_cost: float
    max_rounds: int = 1


@dataclass(frozen=True, slots=True)
class RagDagExecutionContext:
    """Execution identity, scope, permission and budgets for one DAG run."""

    execution_id: str
    correlation_id: str
    actor_id: str
    module_id: str
    request_id: str
    decision_id: str
    module_ids: tuple[str, ...]
    data_categories: tuple[str, ...]
    permission_scope: str
    budgets: RagDagBudgets
    created_at: float
    cancel_requested: bool = False

    def to_record(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "correlation_id": self.correlation_id,
            "actor_id": self.actor_id,
            "module_id": self.module_id,
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "module_ids": list(self.module_ids),
            "data_categories": list(self.data_categories),
            "permission_scope": self.permission_scope,
            "budgets": {
                "max_steps": self.budgets.max_steps,
                "max_seconds": self.budgets.max_seconds,
                "max_cost": self.budgets.max_cost,
                "max_rounds": self.budgets.max_rounds,
            },
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RagDagPlan:
    """A validated, bounded DAG ready for execution."""

    dag_id: str
    kind: RagDagKind
    nodes: tuple[RagDagNode, ...]
    edges: tuple[RagDagEdge, ...]
    context: RagDagExecutionContext

    def node(self, node_id: str) -> RagDagNode:
        for candidate in self.nodes:
            if candidate.node_id == node_id:
                return candidate
        raise KeyError(f"unknown-node:{node_id}")

    def to_record(self) -> dict[str, Any]:
        return {
            "dag_id": self.dag_id,
            "kind": self.kind.value,
            "nodes": [node.to_record() for node in self.nodes],
            "edges": [edge.to_record() for edge in self.edges],
            "context": self.context.to_record(),
        }


@dataclass(frozen=True, slots=True)
class RagDagNodeResult:
    """One executed node's outcome."""

    node_id: str
    node_type: RagDagNodeType
    state: RagDagState
    attempts: int
    latency_ms: int
    evidence: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type.value,
            "state": self.state.value,
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
            "evidence": dict(self.evidence),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class RagDagExecutionResult:
    """Aggregated execution result with evidence and correlation identity."""

    dag_id: str
    kind: RagDagKind
    state: RagDagState
    execution_id: str
    correlation_id: str
    node_results: tuple[RagDagNodeResult, ...]
    latency_ms: int
    evidence_digest: str
    failure_reasons: tuple[str, ...] = ()
    compensations: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "dag_id": self.dag_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "execution_id": self.execution_id,
            "correlation_id": self.correlation_id,
            "node_results": [result.to_record() for result in self.node_results],
            "latency_ms": self.latency_ms,
            "evidence_digest": self.evidence_digest,
            "failure_reasons": list(self.failure_reasons),
            "compensations": dict(self.compensations),
        }


__all__ = [
    "REGISTERED_NODE_CATALOG",
    "TERMINAL_STATES",
    "RagDagBudgets",
    "RagDagEdge",
    "RagDagExecutionContext",
    "RagDagExecutionResult",
    "RagDagKind",
    "RagDagNode",
    "RagDagNodeResult",
    "RagDagNodeSpec",
    "RagDagNodeType",
    "RagDagPlan",
    "RagDagState",
    "node_spec",
]

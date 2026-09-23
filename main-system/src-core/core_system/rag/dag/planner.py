"""RAG DAG planner — bounded, validated DAG construction (A549).

The planner builds the five bounded DAG kinds (query, multi-RAG, index,
rebuild, repair) from the registered node catalog and rejects any plan that
is cyclic, uses an unregistered node type, declares an unauthorized
dependency, omits a declared required input, needs a permission scope it
does not carry, or leaves its budgets unbounded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping

from .contracts import (
    REGISTERED_NODE_CATALOG,
    RagDagBudgets,
    RagDagEdge,
    RagDagExecutionContext,
    RagDagKind,
    RagDagNode,
    RagDagNodeSpec,
    RagDagNodeType,
    RagDagPlan,
    node_spec,
)


class RagDagPlanRejected(ValueError):
    """Fail-closed rejection of an invalid or unbounded DAG plan."""

    failure_code = "RAG_DAG_PLAN_REJECTED"


HARD_MAX_STEPS = 64
HARD_MAX_SECONDS = 600.0
HARD_MAX_COST = 10_000.0
HARD_MAX_ROUNDS = 3

KIND_REQUIRED_NODE_TYPES: Mapping[RagDagKind, tuple[RagDagNodeType, ...]] = {
    RagDagKind.QUERY: (
        RagDagNodeType.RETRIEVAL,
        RagDagNodeType.FUSION,
        RagDagNodeType.MODEL_INFERENCE,
        RagDagNodeType.CITATION_VALIDATION,
    ),
    RagDagKind.RETRIEVAL_CHAIN: (
        RagDagNodeType.RETRIEVAL,
        RagDagNodeType.FUSION,
        RagDagNodeType.CONTEXT_BUILD,
    ),
    RagDagKind.MULTI_RAG: (
        RagDagNodeType.RETRIEVAL,
        RagDagNodeType.FUSION,
        RagDagNodeType.MODEL_INFERENCE,
        RagDagNodeType.CITATION_VALIDATION,
    ),
    RagDagKind.INDEX: (RagDagNodeType.INDEX, RagDagNodeType.PUBLISH_BARRIER),
    RagDagKind.REBUILD: (RagDagNodeType.INDEX, RagDagNodeType.PUBLISH_BARRIER),
    RagDagKind.REPAIR: (
        RagDagNodeType.REPAIR,
        RagDagNodeType.VERIFICATION,
        RagDagNodeType.PUBLISH_BARRIER,
    ),
}

KIND_ORDERING: Mapping[RagDagKind, tuple[tuple[RagDagNodeType, RagDagNodeType], ...]] = {
    RagDagKind.QUERY: (
        (RagDagNodeType.CACHE_LOOKUP, RagDagNodeType.CACHE_VALIDATE),
        (RagDagNodeType.CACHE_VALIDATE, RagDagNodeType.RETRIEVAL),
        (RagDagNodeType.RETRIEVAL, RagDagNodeType.FUSION),
        (RagDagNodeType.FUSION, RagDagNodeType.RERANK),
        (RagDagNodeType.RERANK, RagDagNodeType.CONTEXT_BUILD),
        (RagDagNodeType.CONTEXT_BUILD, RagDagNodeType.MODEL_INFERENCE),
        (RagDagNodeType.MODEL_INFERENCE, RagDagNodeType.CITATION_VALIDATION),
    ),
    RagDagKind.RETRIEVAL_CHAIN: (
        (RagDagNodeType.RETRIEVAL, RagDagNodeType.FUSION),
        (RagDagNodeType.FUSION, RagDagNodeType.RERANK),
        (RagDagNodeType.RERANK, RagDagNodeType.CONTEXT_BUILD),
    ),
    RagDagKind.MULTI_RAG: (
        (RagDagNodeType.CACHE_LOOKUP, RagDagNodeType.CACHE_VALIDATE),
        (RagDagNodeType.CACHE_VALIDATE, RagDagNodeType.RETRIEVAL),
        (RagDagNodeType.RETRIEVAL, RagDagNodeType.FUSION),
        (RagDagNodeType.FUSION, RagDagNodeType.RERANK),
        (RagDagNodeType.RERANK, RagDagNodeType.CONTEXT_BUILD),
        (RagDagNodeType.CONTEXT_BUILD, RagDagNodeType.MODEL_INFERENCE),
        (RagDagNodeType.MODEL_INFERENCE, RagDagNodeType.CITATION_VALIDATION),
    ),
    RagDagKind.INDEX: ((RagDagNodeType.INDEX, RagDagNodeType.PUBLISH_BARRIER),),
    RagDagKind.REBUILD: (
        (RagDagNodeType.INDEX, RagDagNodeType.VERIFICATION),
        (RagDagNodeType.VERIFICATION, RagDagNodeType.PUBLISH_BARRIER),
    ),
    RagDagKind.REPAIR: (
        (RagDagNodeType.REPAIR, RagDagNodeType.VERIFICATION),
        (RagDagNodeType.VERIFICATION, RagDagNodeType.PUBLISH_BARRIER),
    ),
}


@dataclass(frozen=True, slots=True)
class RagDagPlanRequest:
    """Caller-declared inputs for one bounded DAG plan."""

    kind: RagDagKind
    dag_id: str
    module_ids: tuple[str, ...]
    data_categories: tuple[str, ...]
    rag_types: tuple[str, ...] = ("hybrid",)
    resource_ids: tuple[str, ...] = ()
    repair_target: str = ""


class RagDagPlanner:
    """Builds and validates the five bounded DAG kinds."""

    def __init__(self, catalog: Mapping[RagDagNodeType, RagDagNodeSpec] | None = None) -> None:
        self._catalog = dict(catalog or REGISTERED_NODE_CATALOG)

    def plan(
        self,
        request: RagDagPlanRequest,
        context: RagDagExecutionContext,
    ) -> RagDagPlan:
        builders = {
            RagDagKind.QUERY: self._build_query,
            RagDagKind.MULTI_RAG: self._build_multi_rag,
            RagDagKind.RETRIEVAL_CHAIN: self._build_retrieval_chain,
            RagDagKind.INDEX: self._build_index,
            RagDagKind.REBUILD: self._build_rebuild,
            RagDagKind.REPAIR: self._build_repair,
        }
        builder = builders.get(request.kind)
        if builder is None:
            raise RagDagPlanRejected(f"unregistered-dag-kind:{request.kind}")
        plan = builder(request, context)
        self.validate(plan)
        return plan

    def validate(self, plan: RagDagPlan) -> None:
        """Fail closed on any structural or declarative violation."""
        if not plan.dag_id:
            raise RagDagPlanRejected("dag-id-required")
        if not plan.nodes:
            raise RagDagPlanRejected("empty-dag")
        if plan.context.cancel_requested:
            raise RagDagPlanRejected("execution-cancelled")

        node_ids = [node.node_id for node in plan.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise RagDagPlanRejected("duplicate-node-id")
        known = set(node_ids)

        for node in plan.nodes:
            if node.node_type not in self._catalog:
                label = getattr(node.node_type, "value", node.node_type)
                raise RagDagPlanRejected(f"unregistered-node-type:{label}")
            spec = self._catalog[node.node_type]
            missing = [key for key in spec.required_inputs if key not in node.inputs]
            if missing:
                raise RagDagPlanRejected(
                    f"missing-required-input:{node.node_id}:{','.join(missing)}"
                )
            if spec.permission_scope_required and not plan.context.permission_scope:
                raise RagDagPlanRejected(
                    f"permission-scope-required:{node.node_id}"
                )

        for edge in plan.edges:
            if edge.source_id not in known or edge.target_id not in known:
                raise RagDagPlanRejected(
                    f"dangling-edge:{edge.source_id}->{edge.target_id}"
                )
            if edge.source_id == edge.target_id:
                raise RagDagPlanRejected(f"self-edge:{edge.source_id}")
            if not edge.authorized:
                raise RagDagPlanRejected(
                    f"unauthorized-dependency:{edge.source_id}->{edge.target_id}"
                )

        self._assert_acyclic(plan)
        self._assert_ordering(plan)
        self._assert_bounded(plan)

        present_types = {node.node_type for node in plan.nodes}
        for required in KIND_REQUIRED_NODE_TYPES[plan.kind]:
            if required not in present_types:
                raise RagDagPlanRejected(
                    f"missing-required-node:{plan.kind.value}:{required.value}"
                )

    def _assert_acyclic(self, plan: RagDagPlan) -> None:
        indegree = {node.node_id: 0 for node in plan.nodes}
        adjacency: dict[str, list[str]] = {node.node_id: [] for node in plan.nodes}
        for edge in plan.edges:
            adjacency[edge.source_id].append(edge.target_id)
            indegree[edge.target_id] += 1
        queue = [node_id for node_id, degree in indegree.items() if degree == 0]
        visited = 0
        while queue:
            node_id = queue.pop()
            visited += 1
            for target in adjacency[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(plan.nodes):
            raise RagDagPlanRejected("dag-cycle")

    def _assert_ordering(self, plan: RagDagPlan) -> None:
        for source_type, target_type in KIND_ORDERING[plan.kind]:
            sources = {
                node.node_id for node in plan.nodes if node.node_type == source_type
            }
            targets = {
                node.node_id for node in plan.nodes if node.node_type == target_type
            }
            for target_id in targets:
                target_node = plan.node(target_id)
                incoming = {edge.source_id for edge in plan.edges if edge.target_id == target_id}
                if not incoming:
                    raise RagDagPlanRejected(
                        f"bypass-dependency:{source_type.value}->{target_type.value}"
                    )
                if not incoming & sources:
                    raise RagDagPlanRejected(
                        f"bypass-dependency:{source_type.value}->{target_type.value}"
                    )

    def _assert_bounded(self, plan: RagDagPlan) -> None:
        budgets = plan.context.budgets
        if budgets.max_steps < len(plan.nodes):
            raise RagDagPlanRejected("budget-steps-too-small")
        if not 0 < budgets.max_steps <= HARD_MAX_STEPS:
            raise RagDagPlanRejected("budget-steps-out-of-envelope")
        if not 0 < budgets.max_seconds <= HARD_MAX_SECONDS:
            raise RagDagPlanRejected("budget-seconds-out-of-envelope")
        if not 0 <= budgets.max_cost <= HARD_MAX_COST:
            raise RagDagPlanRejected("budget-cost-out-of-envelope")
        if not 1 <= budgets.max_rounds <= HARD_MAX_ROUNDS:
            raise RagDagPlanRejected("budget-rounds-out-of-envelope")

    def _build_query(
        self, request: RagDagPlanRequest, context: RagDagExecutionContext
    ) -> RagDagPlan:
        nodes = (
            self._node(
                "cache-lookup",
                RagDagNodeType.CACHE_LOOKUP,
                {"cache_key": f"{request.dag_id}:{request.module_ids}"},
            ),
            self._node(
                "cache-validate",
                RagDagNodeType.CACHE_VALIDATE,
                {"cache_entry": "cache-lookup.cache_entry"},
                dependencies=("cache-lookup",),
            ),
            self._node(
                "retrieval",
                RagDagNodeType.RETRIEVAL,
                {
                    "query": "$query",
                    "module_ids": list(request.module_ids),
                    "scope": context.permission_scope,
                },
                dependencies=("cache-validate",),
            ),
            self._node(
                "fusion",
                RagDagNodeType.FUSION,
                {"candidate_sets": ["retrieval.candidates"]},
                dependencies=("retrieval",),
            ),
            self._node(
                "rerank",
                RagDagNodeType.RERANK,
                {"fused_candidates": "fusion.fused_candidates", "reranker_limit": 20},
                dependencies=("fusion",),
            ),
            self._node(
                "context-build",
                RagDagNodeType.CONTEXT_BUILD,
                {
                    "reranked_candidates": "rerank.reranked_candidates",
                    "max_context_tokens": 8000,
                },
                dependencies=("rerank",),
            ),
            self._node(
                "model-inference",
                RagDagNodeType.MODEL_INFERENCE,
                {"context_text": "context-build.context_text", "model_identity": "$model"},
                dependencies=("context-build",),
            ),
            self._node(
                "citation-validation",
                RagDagNodeType.CITATION_VALIDATION,
                {"answer_text": "model-inference.answer_text", "citations": "$citations"},
                dependencies=("model-inference",),
            ),
        )
        return self._plan(request, context, nodes)

    def _build_multi_rag(
        self, request: RagDagPlanRequest, context: RagDagExecutionContext
    ) -> RagDagPlan:
        rag_types = request.rag_types or ("hybrid",)
        retrieval_nodes = tuple(
            self._node(
                f"retrieval-{rag_type}",
                RagDagNodeType.RETRIEVAL,
                {
                    "query": "$query",
                    "module_ids": list(request.module_ids),
                    "scope": context.permission_scope,
                    "rag_type": rag_type,
                },
                dependencies=("cache-validate",),
            )
            for rag_type in rag_types
        )
        retrieval_ids = tuple(node.node_id for node in retrieval_nodes)
        nodes = (
            self._node(
                "cache-lookup",
                RagDagNodeType.CACHE_LOOKUP,
                {"cache_key": f"{request.dag_id}:{request.module_ids}"},
            ),
            self._node(
                "cache-validate",
                RagDagNodeType.CACHE_VALIDATE,
                {"cache_entry": "cache-lookup.cache_entry"},
                dependencies=("cache-lookup",),
            ),
            *retrieval_nodes,
            self._node(
                "fusion",
                RagDagNodeType.FUSION,
                {"candidate_sets": [f"{node_id}.candidates" for node_id in retrieval_ids]},
                dependencies=retrieval_ids,
            ),
            self._node(
                "rerank",
                RagDagNodeType.RERANK,
                {"fused_candidates": "fusion.fused_candidates", "reranker_limit": 20},
                dependencies=("fusion",),
            ),
            self._node(
                "context-build",
                RagDagNodeType.CONTEXT_BUILD,
                {
                    "reranked_candidates": "rerank.reranked_candidates",
                    "max_context_tokens": 8000,
                },
                dependencies=("rerank",),
            ),
            self._node(
                "model-inference",
                RagDagNodeType.MODEL_INFERENCE,
                {"context_text": "context-build.context_text", "model_identity": "$model"},
                dependencies=("context-build",),
            ),
            self._node(
                "citation-validation",
                RagDagNodeType.CITATION_VALIDATION,
                {"answer_text": "model-inference.answer_text", "citations": "$citations"},
                dependencies=("model-inference",),
            ),
        )
        return self._plan(request, context, nodes)

    def _build_retrieval_chain(
        self, request: RagDagPlanRequest, context: RagDagExecutionContext
    ) -> RagDagPlan:
        """retrieval → fusion → rerank → context-build — the honest
        retrieval-plane chain; no inference/citation nodes because the
        retrieval layer must never fabricate answer evidence."""
        nodes = (
            self._node(
                "retrieval",
                RagDagNodeType.RETRIEVAL,
                {
                    "query": "$query",
                    "module_ids": list(request.module_ids),
                    "scope": context.permission_scope,
                },
            ),
            self._node(
                "fusion",
                RagDagNodeType.FUSION,
                {"candidate_sets": ["retrieval.candidates"]},
                dependencies=("retrieval",),
            ),
            self._node(
                "rerank",
                RagDagNodeType.RERANK,
                {"fused_candidates": "fusion.fused_candidates", "reranker_limit": 20},
                dependencies=("fusion",),
            ),
            self._node(
                "context-build",
                RagDagNodeType.CONTEXT_BUILD,
                {
                    "reranked_candidates": "rerank.reranked_candidates",
                    "max_context_tokens": 8000,
                },
                dependencies=("rerank",),
            ),
        )
        return self._plan(request, context, nodes)

    def _build_index(
        self, request: RagDagPlanRequest, context: RagDagExecutionContext
    ) -> RagDagPlan:
        resource_ids = request.resource_ids or ("$resource",)
        nodes = (
            self._node(
                "index",
                RagDagNodeType.INDEX,
                {
                    "resource_id": list(resource_ids),
                    "content_hash": "$content_hash",
                    "generation_id": "$generation",
                },
            ),
            self._node(
                "publish-barrier",
                RagDagNodeType.PUBLISH_BARRIER,
                {
                    "publish_target": "$generation",
                    "verification_result": "$index_receipt",
                },
                dependencies=("index",),
            ),
        )
        return self._plan(request, context, nodes)

    def _build_rebuild(
        self, request: RagDagPlanRequest, context: RagDagExecutionContext
    ) -> RagDagPlan:
        nodes = (
            self._node(
                "index",
                RagDagNodeType.INDEX,
                {
                    "resource_id": list(request.resource_ids or ("$resource",)),
                    "content_hash": "$content_hash",
                    "generation_id": "$generation",
                },
            ),
            self._node(
                "verification",
                RagDagNodeType.VERIFICATION,
                {"verification_target": "$generation"},
                dependencies=("index",),
            ),
            self._node(
                "publish-barrier",
                RagDagNodeType.PUBLISH_BARRIER,
                {
                    "publish_target": "$generation",
                    "verification_result": "verification.verification_result",
                },
                dependencies=("verification",),
            ),
        )
        return self._plan(request, context, nodes)

    def _build_repair(
        self, request: RagDagPlanRequest, context: RagDagExecutionContext
    ) -> RagDagPlan:
        nodes = (
            self._node(
                "repair",
                RagDagNodeType.REPAIR,
                {"repair_target": request.repair_target or "$target", "repair_plan": "$plan"},
            ),
            self._node(
                "verification",
                RagDagNodeType.VERIFICATION,
                {"verification_target": request.repair_target or "$target"},
                dependencies=("repair",),
            ),
            self._node(
                "publish-barrier",
                RagDagNodeType.PUBLISH_BARRIER,
                {
                    "publish_target": request.repair_target or "$target",
                    "verification_result": "verification.verification_result",
                },
                dependencies=("verification",),
            ),
        )
        return self._plan(request, context, nodes)

    def _node(
        self,
        node_id: str,
        node_type: RagDagNodeType,
        inputs: Mapping[str, object],
        dependencies: tuple[str, ...] = (),
    ) -> RagDagNode:
        node_spec(node_type)
        return RagDagNode(
            node_id=node_id,
            node_type=node_type,
            inputs=dict(inputs),
            dependencies=dependencies,
        )

    def _plan(
        self,
        request: RagDagPlanRequest,
        context: RagDagExecutionContext,
        nodes: tuple[RagDagNode, ...],
    ) -> RagDagPlan:
        edges: list[RagDagEdge] = []
        for node in nodes:
            for dependency in node.dependencies:
                edges.append(RagDagEdge(source_id=dependency, target_id=node.node_id))
        return RagDagPlan(
            dag_id=request.dag_id,
            kind=request.kind,
            nodes=nodes,
            edges=tuple(edges),
            context=context,
        )


def default_budgets(*, max_steps: int = 16, max_seconds: float = 60.0) -> RagDagBudgets:
    """Convenience budgets inside the hard envelope."""
    return RagDagBudgets(
        max_steps=max(1, min(max_steps, HARD_MAX_STEPS)),
        max_seconds=max(0.1, min(max_seconds, HARD_MAX_SECONDS)),
        max_cost=100.0,
        max_rounds=1,
    )


def execution_context(
    *,
    execution_id: str,
    correlation_id: str,
    actor_id: str,
    module_id: str,
    request_id: str,
    decision_id: str,
    module_ids: tuple[str, ...],
    data_categories: tuple[str, ...],
    permission_scope: str,
    budgets: RagDagBudgets | None = None,
) -> RagDagExecutionContext:
    """Build an execution context with sane bounded defaults."""
    return RagDagExecutionContext(
        execution_id=execution_id,
        correlation_id=correlation_id,
        actor_id=actor_id,
        module_id=module_id,
        request_id=request_id,
        decision_id=decision_id,
        module_ids=module_ids,
        data_categories=data_categories,
        permission_scope=permission_scope,
        budgets=budgets or default_budgets(),
        created_at=time.time(),
    )


__all__ = [
    "HARD_MAX_COST",
    "HARD_MAX_ROUNDS",
    "HARD_MAX_SECONDS",
    "HARD_MAX_STEPS",
    "KIND_ORDERING",
    "KIND_REQUIRED_NODE_TYPES",
    "RagDagPlanRejected",
    "RagDagPlanRequest",
    "RagDagPlanner",
    "default_budgets",
    "execution_context",
]

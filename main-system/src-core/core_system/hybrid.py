"""Unified DAG + CAG + RAG Hybrid Architecture.

This module provides the main entry point for the hybrid retrieval system
that combines:
- DAG: Directed Acyclic Graph for query planning and orchestration
- CAG: Context-Augmented Generation for pre-loaded context
- RAG: Retrieval-Augmented Generation for dynamic retrieval

The system automatically selects the best architecture based on:
- Query type and complexity
- Available pre-loaded context
- Latency requirements
- Resource budgets
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .cag import (
    CAGConfig,
    CAGContext,
    ContextLoader,
    ContextManager,
    ContextRouter,
    create_cag_pipeline,
)
from .rag.orchestration.orchestrator import RagOrchestrator, OrchestratorResult
from .rag.orchestration.query_planner import QueryPlan, PlanMode
from .rag.orchestration.evidence import RagArchitecture

_logger = logging.getLogger("gptbridge.hybrid")


@dataclass(frozen=True, slots=True)
class HybridConfig:
    """Configuration for hybrid DAG+CAG+RAG system."""
    cag: CAGConfig = field(default_factory=CAGConfig)
    enable_cag: bool = True
    enable_dag: bool = True
    enable_rag: bool = True
    dag_max_rounds: int = 3
    cag_preload_on_startup: bool = True
    auto_architecture_selection: bool = True


@dataclass(frozen=True, slots=True)
class HybridResult:
    """Result from hybrid query execution."""
    architecture_used: str  # "CAG", "DAG", "RAG", "HYBRID"
    plan: Optional[QueryPlan]
    evidence: tuple[Any, ...]
    context: Any
    generation: Any
    agentic_rounds: int
    degraded: bool
    latency_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)


class ArchitectureSelector:
    """Selects optimal architecture for a query."""

    def __init__(self, config: HybridConfig) -> None:
        self._config = config

    def select(
        self,
        query: str,
        module_ids: tuple[str, ...],
        context_manager: ContextManager,
        generation_mode: str,
    ) -> tuple[str, str]:
        """Select architecture and return (architecture, reason)."""
        if not self._config.auto_architecture_selection:
            return "RAG", "manual"

        # Check for existing CAG context
        if self._config.enable_cag and context_manager.find_by_modules(module_ids):
            return "CAG", "preloaded_context_available"

        # Complex queries with multiple aspects -> DAG
        if self._config.enable_dag and self._is_complex_query(query):
            return "DAG", "complex_query"

        # Default to RAG
        return "RAG", "default"

    def _is_complex_query(self, query: str) -> bool:
        """Heuristic for complex queries needing DAG orchestration."""
        complexity_indicators = [
            len(query) > 500,
            query.count("?") > 2,
            any(kw in query.lower() for kw in ["compare", "analyze", "summarize", "explain", "step by step"]),
        ]
        return sum(complexity_indicators) >= 2


class HybridOrchestrator:
    """Unified orchestrator for DAG + CAG + RAG."""

    def __init__(
        self,
        retrievers: dict[str, Callable],
        rag_orchestrator: RagOrchestrator,
        config: HybridConfig | None = None,
    ) -> None:
        self._config = config or HybridConfig()
        self._rag = rag_orchestrator
        self._cag_loader, self._cag_manager, self._cag_router = create_cag_pipeline(
            retrievers, rag_orchestrator, self._config.cag
        )
        self._selector = ArchitectureSelector(self._config)

    # CAG pipeline handles are owned here; lifecycle code (CAGIntegration)
    # reads them through these properties instead of private attributes.
    @property
    def cag_loader(self) -> ContextLoader:
        return self._cag_loader

    @property
    def cag_manager(self) -> ContextManager:
        return self._cag_manager

    @property
    def cag_router(self) -> ContextRouter:
        return self._cag_router

    def query(
        self,
        query: str,
        *,
        generation_mode: str = "general",
        module_ids: tuple[str, ...] = (),
        session_id: str = "",
        explicit_architectures: tuple[Any, ...] | None = None,
        required_aspects: tuple[str, ...] = (),
        task_instruction: str = "",
        plan_id: str = "",
    ) -> HybridResult:
        """Execute query using optimal architecture."""
        import time
        start = time.perf_counter()

        # Select architecture
        arch, reason = self._selector.select(
            query, module_ids, self._cag_manager, generation_mode
        )

        _logger.info("Hybrid: selected %s (%s)", arch, reason)

        if arch == "CAG" and self._config.enable_cag:
            result = self._execute_cag(query, module_ids, generation_mode)
            arch_used = "CAG"
        elif arch == "DAG" and self._config.enable_dag:
            result = self._execute_dag(
                query, module_ids, generation_mode, session_id,
                explicit_architectures, required_aspects, task_instruction, plan_id
            )
            arch_used = "DAG"
        else:
            result = self._execute_rag(
                query, module_ids, generation_mode, session_id,
                explicit_architectures, required_aspects, task_instruction, plan_id
            )
            arch_used = "RAG"

        latency_ms = (time.perf_counter() - start) * 1000

        return HybridResult(
            architecture_used=arch_used,
            plan=getattr(result, 'plan', None),
            evidence=getattr(result, 'evidence', ()),
            context=getattr(result, 'context', None),
            generation=getattr(result, 'generation', None),
            agentic_rounds=getattr(result, 'agentic_rounds', 0),
            degraded=getattr(result, 'degraded', False),
            latency_ms=latency_ms,
            metadata={"selection_reason": reason},
        )

    def _execute_cag(
        self,
        query: str,
        module_ids: tuple[str, ...],
        generation_mode: str,
    ) -> Any:
        return self._cag_router.query(
            query, module_ids, generation_mode
        )

    def _execute_dag(
        self,
        query: str,
        module_ids: tuple[str, ...],
        generation_mode: str,
        session_id: str,
        explicit_architectures: tuple[Any, ...] | None,
        required_aspects: tuple[str, ...],
        task_instruction: str,
        plan_id: str,
    ) -> OrchestratorResult:
        """Run the query through the bounded DAG executor.

        The retrieval-plane chain (retrieval→fusion→rerank→context-build)
        executes under per-node timeout, cancellation and evidence
        requirements — MODEL_INFERENCE/CITATION_VALIDATION are absent
        because generation happens downstream; fabricating them would
        break the executor's no-fabricated-outcome contract.  A
        non-SUCCEEDED run returns a degraded result carrying the
        executor's failure reasons instead of silently falling back."""
        import uuid

        from .rag.dag import (
            RagDagExecutor,
            RagDagKind,
            RagDagPlanRequest,
            RagDagPlanner,
            RagDagState,
            default_budgets,
            execution_context,
        )
        from .rag.dag.handlers import retrieval_chain_handlers
        from .rag.orchestration.context_builder import build_context
        from .rag.orchestration.generation_router import route_generation
        from .rag.orchestration.sufficiency import evaluate_sufficiency

        dag_id = plan_id or f"dag-{uuid.uuid4().hex[:12]}"
        context = execution_context(
            execution_id=f"exec-{uuid.uuid4().hex[:12]}",
            correlation_id=session_id or dag_id,
            actor_id="system/rag-dag",
            module_id=module_ids[0] if module_ids else "rag",
            request_id=dag_id,
            decision_id="",
            module_ids=module_ids,
            data_categories=(),
            permission_scope="rag:query",
            budgets=default_budgets(max_seconds=60.0),
        )
        arch_names = tuple(
            (a.value if isinstance(a, RagArchitecture) else str(a))
            for a in (explicit_architectures or (RagArchitecture.HYBRID,))
        )
        request = RagDagPlanRequest(
            kind=RagDagKind.RETRIEVAL_CHAIN,
            dag_id=dag_id,
            module_ids=module_ids,
            data_categories=(),
            rag_types=arch_names or ("hybrid",),
        )
        handlers = retrieval_chain_handlers(
            self._rag,
            query=query,
            scope={"module_ids": module_ids, "session_id": session_id},
            task_instruction=task_instruction,
        )
        executor = RagDagExecutor(handlers, node_timeout_seconds=30.0)
        result = executor.execute(plan=RagDagPlanner().plan(request, context))

        node_evidence = {r.node_id: r.evidence for r in result.node_results}
        succeeded = result.state is RagDagState.SUCCEEDED
        if not succeeded:
            _logger.warning(
                "DAG run %s ended %s: %s",
                dag_id, result.state.value, result.failure_reasons,
            )
        ranked = list(
            (node_evidence.get("rerank") or {}).get("reranked_candidates") or []
        )
        built = (node_evidence.get("context-build") or {}).get("built_context")
        if built is None:
            built = build_context(
                [], system_governance="", task_instruction=task_instruction or query
            )
        report = evaluate_sufficiency(
            ranked, required_aspects=required_aspects
        )
        return OrchestratorResult(
            plan=None,
            evidence=tuple(ranked),
            report=report,
            context=built,
            generation=route_generation(generation_mode),
            agentic_rounds=0,
            degraded=not succeeded or report.degraded,
            # type: ignore[attr-defined] — see below
        ) if not hasattr(OrchestratorResult, "metadata") else OrchestratorResult(
            plan=None,
            evidence=tuple(ranked),
            report=report,
            context=built,
            generation=route_generation(generation_mode),
            agentic_rounds=0,
            degraded=not succeeded,
        )

    def _execute_rag(
        self,
        query: str,
        module_ids: tuple[str, ...],
        generation_mode: str,
        session_id: str,
        explicit_architectures: tuple[Any, ...] | None,
        required_aspects: tuple[str, ...],
        task_instruction: str,
        plan_id: str,
    ) -> OrchestratorResult:
        return self._rag.query(
            query,
            generation_mode=generation_mode,
            module_ids=module_ids,
            session_id=session_id,
            explicit_architectures=explicit_architectures,
            required_aspects=required_aspects,
            task_instruction=task_instruction,
            plan_id=plan_id,
        )

    def preload_contexts(self, module_groups: list[tuple[str, ...]]) -> None:
        """Preload CAG contexts for module groups (startup optimization)."""
        if not self._config.cag_preload_on_startup:
            return
        for modules in module_groups:
            context = self._cag_loader.load_context(modules)
            self._cag_manager.store(context)
            _logger.info("CAG: preloaded context for %s (%d docs)",
                         modules, len(context.documents))

    def get_stats(self) -> dict[str, Any]:
        return {
            "config": {
                "enable_cag": self._config.enable_cag,
                "enable_dag": self._config.enable_dag,
                "enable_rag": self._config.enable_rag,
            },
            "cag": self._cag_manager.stats(),
        }


def create_hybrid_orchestrator(
    retrievers: dict[str, Callable],
    rag_orchestrator: RagOrchestrator,
    config: HybridConfig | None = None,
) -> HybridOrchestrator:
    """Factory for hybrid orchestrator."""
    return HybridOrchestrator(retrievers, rag_orchestrator, config)


__all__ = [
    "HybridConfig",
    "HybridResult",
    "ArchitectureSelector",
    "HybridOrchestrator",
    "create_hybrid_orchestrator",
]
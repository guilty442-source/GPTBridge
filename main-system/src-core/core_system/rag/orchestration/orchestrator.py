"""RAG Orchestrator — the formal four-architecture pipeline.

    Query
      -> Scope Resolver (module/data-category scope)
      -> Query Planner (STATIC | ADAPTIVE, multi-arch tuple)
      -> Hybrid / Code / Memory retrievers
      -> Evidence Pool -> Fusion -> Reranker
      -> Evidence Sufficiency
            YES -> Context Builder -> Generation Router -> LLM
            NO  -> Agentic Controller (bounded re-retrieval)
      -> Citation Validation -> Answer

All retriever outputs are normalised to ``RagEvidence`` upstream
(adapters); this layer only sees the unified contract.
"""
from __future__ import annotations

import concurrent.futures
import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .agentic_controller import ALLOWED_TOOLS, AgenticController
from .context_builder import BuiltContext, build_context
from .evidence import RagArchitecture, RagEvidence, make_citation
from .fusion import architecture_fusion, mark_conflicts
from .generation_router import GenerationDecision, route_generation
from .query_planner import PlanMode, QueryPlan, plan_query
from .sufficiency import (
    SufficiencyPolicy,
    SufficiencyReport,
    SufficiencyVerdict,
    evaluate_sufficiency,
)

# retriever callable: (query, scope) -> [RagEvidence]
RetrieverFn = Callable[[str, dict[str, Any]], list[RagEvidence]]
RerankFn = Callable[[str, list[RagEvidence]], list[RagEvidence]]

_ARCH_TOOL = {
    RagArchitecture.HYBRID: "retrieve_hybrid",
    RagArchitecture.CODE: "retrieve_code",
    RagArchitecture.MEMORY: "retrieve_memory",
}


@dataclass(frozen=True, slots=True)
class OrchestratorResult:
    plan: QueryPlan
    evidence: tuple[RagEvidence, ...]
    report: SufficiencyReport
    context: BuiltContext
    generation: GenerationDecision
    agentic_rounds: int = 0
    degraded: bool = False


class RagOrchestrator:
    """Wires planner, retrievers, fusion, gate, agentic, context."""

    def __init__(
        self,
        retrievers: dict[str, RetrieverFn],
        *,
        policy: SufficiencyPolicy = SufficiencyPolicy(),
        reranker: Optional[RerankFn] = None,
        reformulator: Any = None,
        system_governance: str = "",
    ) -> None:
        unknown = set(retrievers) - set(ALLOWED_TOOLS)
        if unknown:
            raise ValueError(f"non-formal retriever keys: {sorted(unknown)}")
        self._retrievers = dict(retrievers)
        self._policy = policy
        self._reranker = reranker
        self._reformulator = reformulator
        self._system = system_governance

    @property
    def reranker(self) -> Optional[RerankFn]:
        """The configured reranker (read-only) — consumed by the DAG
        retrieval-chain RERANK node so it applies the same governed
        reranker instead of owning a second one."""
        return self._reranker

    @property
    def policy(self) -> SufficiencyPolicy:
        return self._policy

    def dispatch(
        self, archs: tuple[RagArchitecture, ...], query: str, scope: dict[str, Any]
    ) -> dict[RagArchitecture, list[RagEvidence]]:
        """Per-architecture retrieval fan-out — the public boundary other
        subsystems (CAG preload) consume instead of reaching into
        orchestrator internals.

        Multi-architecture plans run their lanes concurrently under the
        shared core thread budget (retrievers are independent blocking
        IO); a single-architecture plan stays on the caller thread.
        Exceptions propagate exactly as the serial path did."""
        lanes: list[tuple[RagArchitecture, RetrieverFn]] = []
        for arch in archs:
            tool = _ARCH_TOOL.get(arch)
            fn = self._retrievers.get(tool) if tool else None
            if fn is not None:
                lanes.append((arch, fn))
        if len(lanes) <= 1:
            return {arch: fn(query, scope) for arch, fn in lanes}

        from shared_layer.performance.thread_budget import bounded_workers

        pools: dict[RagArchitecture, list[RagEvidence]] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=bounded_workers(len(lanes)),
            thread_name_prefix="rag-dispatch",
        ) as pool:
            futures = {
                arch: pool.submit(fn, query, scope) for arch, fn in lanes
            }
            for arch, future in futures.items():
                pools[arch] = future.result()
        return pools

    def query(
        self,
        query: str,
        *,
        generation_mode: str = "general",
        module_ids: tuple[str, ...] = (),
        session_id: str = "",
        explicit_architectures: tuple[RagArchitecture, ...] | None = None,
        required_aspects: tuple[str, ...] = (),
        task_instruction: str = "",
        plan_id: str = "",
    ) -> OrchestratorResult:
        scope = {"module_ids": module_ids, "session_id": session_id}
        plan = plan_query(
            query,
            plan_id=plan_id,
            generation_mode=generation_mode,
            module_ids=module_ids,
            session_id=session_id,
            explicit_architectures=explicit_architectures,
            max_rounds=self._policy.max_rounds,
        )

        pools = self.dispatch(plan.rag_architectures, query, scope)
        fused = mark_conflicts(architecture_fusion(pools))
        if self._reranker and fused:
            from ..observability import timed_stage
            with timed_stage("reranker"):
                fused = self._reranker(query, fused)
        report = evaluate_sufficiency(
            fused, policy=self._policy, required_aspects=required_aspects
        )

        agentic_rounds = 0
        if report.verdict in (
            SufficiencyVerdict.RETRIEVE,
            SufficiencyVerdict.FIND_AUTHORITY,
        ) and plan.mode is PlanMode.ADAPTIVE:
            controller = AgenticController(
                self._retrievers,
                policy=self._policy,
                reformulator=self._reformulator,
            )
            tools = tuple(
                _ARCH_TOOL[a] for a in plan.rag_architectures if a in _ARCH_TOOL
            ) or ("retrieve_hybrid",)
            result = controller.run(
                query,
                tools_sequence=tools,
                required_aspects=required_aspects,
                scope=scope,
            )
            fused = mark_conflicts(architecture_fusion(
                self._pool_by_arch(result.evidence)
            ))
            if self._reranker and fused:
                from ..observability import timed_stage
                with timed_stage("reranker"):
                    fused = self._reranker(query, fused)
            report = result.final_report
            agentic_rounds = result.rounds

        context = build_context(
            list(fused),
            system_governance=self._system,
            task_instruction=task_instruction or query,
        )
        generation = route_generation(plan.generation_mode)
        return OrchestratorResult(
            plan=plan,
            evidence=tuple(fused),
            report=report,
            context=context,
            generation=generation,
            agentic_rounds=agentic_rounds,
            degraded=report.verdict is SufficiencyVerdict.STOP_INSUFFICIENT,
        )

    @staticmethod
    def _pool_by_arch(
        evidence: tuple[RagEvidence, ...],
    ) -> dict[RagArchitecture, list[RagEvidence]]:
        pools: dict[RagArchitecture, list[RagEvidence]] = {}
        for e in evidence:
            pools.setdefault(e.rag_type, []).append(e)
        return pools


__all__ = ["OrchestratorResult", "RagOrchestrator", "RetrieverFn"]

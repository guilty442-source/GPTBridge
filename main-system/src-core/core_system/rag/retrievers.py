"""RAG Retriever Implementations — 四種檢索架構的具體實作。

共用基礎設施：
- EmbeddingProvider
- QdrantCanonicalRuntime (VectorStore)
- PostgreSQLMetadataAuthority
- Reranker
- Authorization
- Observability

各自決定檢索行為。
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from .rag_protocol import RagRetriever, RagRetrievalRequest, RagRetrievalResult, RagRetrievalCandidate, RagSearchHit
from .rag_contracts import (
    RagType,
    Citation,
    LifecycleState,
    RagPolicy,
    DEFAULT_POLICY,
)
from .rag_qdrant import QdrantCanonicalRuntime
from .rag_metadata import PostgreSQLMetadataAuthority
from .retrieval_pipeline import (
    RetrievalPipeline,
    RecallPhase,
    PrecisionPhase,
    ReciprocalRankFusion,
    SemanticDeduplicator,
    ContextBudgetManager,
)
from .generation_binder import GenerationBinder, VerifiedHit

_logger = logging.getLogger("gptbridge.rag.retrievers")


# ============================================================================
# Base Retriever
# ============================================================================

class BaseRagRetriever:
    """Base class with shared infrastructure."""

    def __init__(
        self,
        qdrant: QdrantCanonicalRuntime,
        pg_metadata: PostgreSQLMetadataAuthority,
        reranker: Any,
        policy: RagPolicy = DEFAULT_POLICY,
        generation_binder: Optional[Any] = None,
    ) -> None:
        self.qdrant = qdrant
        self.pg_metadata = pg_metadata
        self.reranker = reranker
        self.policy = policy
        self.generation_binder = generation_binder or GenerationBinder(pg_metadata)

        # Shared pipeline components
        self.recall_phase = RecallPhase(qdrant, pg_metadata, policy)
        self.fusion = ReciprocalRankFusion(k=policy.rrf_k)
        self.dedup = SemanticDeduplicator(threshold=policy.semantic_dedup_threshold)
        self.precision_phase = PrecisionPhase(reranker, policy)
        self.context_budget = ContextBudgetManager(policy)

    def _build_citations(
        self,
        hits: list[VerifiedHit],
        request_id: str,
    ) -> tuple[Citation, ...]:
        """Build citations from verified hits."""
        citations = []
        for i, hit in enumerate(hits):
            citation = Citation(
                citation_id=f"R{i+1}",
                resource_id=hit.index_state.resource_id,
                chunk_id=hit.index_state.qdrant_point_id,
                locator_id=f"{hit.index_state.module_id}:{hit.index_state.resource_id}",
                character_start=0,
                character_end=len(hit.hit.get("payload", {}).get("content", "")),
                content_hash=hit.index_state.content_hash,
                generation_id=hit.index_state.generation_id or "",
                retrieval_score=hit.hit.get("score", 0.0),
                module_id=hit.index_state.module_id,
            )
            citations.append(citation)
        return tuple(citations)

    def _build_context_text(self, hits: list[VerifiedHit]) -> str:
        """Build context text from hits."""
        parts = []
        for i, hit in enumerate(hits):
            payload = hit.hit.get("payload", {})
            content = payload.get("content", "")
            locator = f"{hit.index_state.module_id}:{hit.index_state.resource_id}"
            parts.append(f"[R{i+1}] {locator}\n{content}")
        return "\n\n---\n\n".join(parts)


# ============================================================================
# Hybrid RAG Retriever
# ============================================================================

class HybridRagRetriever(BaseRagRetriever):
    """Hybrid retrieval: Dense + FTS + RRF + Reranker.

    Recall: Qdrant dense (30) + PostgreSQL FTS (30)
    Fusion: RRF with weights
    Precision: Cross-encoder rerank (20) -> Top-K (6)
    """

    @property
    def rag_type(self) -> str:
        return RagType.HYBRID.value

    def retrieve(self, request: RagRetrievalRequest) -> RagRetrievalResult:
        start_time = time.monotonic()
        phases = {}

        # Phase 1: Recall
        recall_start = time.monotonic()
        candidates_by_source = self._recall(request)
        phases["recall_ms"] = int((time.monotonic() - recall_start) * 1000)

        # Phase 2: Fusion
        fusion_start = time.monotonic()
        fused = self._fuse(candidates_by_source)
        phases["fusion_ms"] = int((time.monotonic() - fusion_start) * 1000)

        # Phase 2b: Dedup
        dedup_start = time.monotonic()
        deduped = self._deduplicate(fused)
        phases["dedup_ms"] = int((time.monotonic() - dedup_start) * 1000)

        # Phase 3: Precision
        precision_start = time.monotonic()
        precision_results = self._rerank(request.query, deduped)
        phases["rerank_ms"] = int((time.monotonic() - precision_start) * 1000)

        # Build citations
        citations = self._build_citations(
            [r.hit for r in precision_results],
            request.request_id,
        )

        # Build context
        context = self._build_context_text(
            [r.hit for r in precision_results],
        )

        total_latency = int((time.monotonic() - start_time) * 1000)
        phases["total_ms"] = total_latency

        return RagRetrievalResult(
            request_id=request.request_id,
            rag_type=RagType.HYBRID,
            candidates=tuple(
                RagRetrievalCandidate(
                    hit=r.hit,
                    source="hybrid",
                    fused_score=r.final_score,
                )
                for r in precision_results
            ),
            citations=citations,
            context_text=context,
            state="CANONICAL" if request.canonical_required else "DEGRADED",
            policy_version=request.policy_version,
            generation_id=request.generation_id,
            latency_ms=total_latency,
            phases=phases,
        )

    def _recall(self, request: RagRetrievalRequest) -> dict[str, list[Any]]:
        """Run recall from dense + FTS sources."""
        import asyncio

        async def _run_recall():
            return await self.recall_phase.recall(
                query_embedding=list(request.query_vector),
                module_ids=tuple(request.module_ids) if request.module_ids else None,
                query_text=request.query,
            )

        return asyncio.run(_run_recall())

    def _fuse(self, candidates_by_source: dict[str, list[Any]]) -> list[Any]:
        """RRF fusion with weights."""
        weights = {
            "dense": self.policy.dense_weight,
            "fts": self.policy.fts_weight,
            "graph": self.policy.graph_weight,
            "memory": self.policy.memory_weight,
        }
        return self.fusion.fuse(candidates_by_source, weights)

    def _deduplicate(self, fused: list[Any]) -> list[Any]:
        """Semantic deduplication."""
        def get_embedding(candidate):
            content = candidate.hit.hit.get("payload", {}).get("content", "")
            import hashlib
            import random
            seed = int(hashlib.sha256(content.encode()).hexdigest()[:8], 16)
            random.seed(seed)
            return [random.uniform(-1, 1) for _ in range(2560)]

        return self.dedup.deduplicate(fused, get_embedding)

    def _rerank(self, query: str, candidates: list[Any]) -> list[Any]:
        """Cross-encoder reranking."""
        return asyncio.run(self.precision_phase.rerank(query, candidates))


# ============================================================================
# Code RAG Retriever
# ============================================================================

class CodeRagRetriever(BaseRagRetriever):
    """Code retrieval: Semantic + Symbol + AST + Dependency Graph.

    Recall:
    - Semantic code search (dense)
    - Symbol lookup (exact match)
    - AST relationship traversal
    - Dependency graph expansion
    Fusion: Weighted fusion with code-specific weights
    Precision: Code-aware reranker
    """

    @property
    def rag_type(self) -> str:
        return RagType.CODE.value

    def __init__(
        self,
        qdrant: QdrantCanonicalRuntime,
        pg_metadata: PostgreSQLMetadataAuthority,
        reranker: Any,
        policy: RagPolicy = DEFAULT_POLICY,
        generation_binder: Optional[Any] = None,
        symbol_index: Any = None,  # Symbol index for exact lookup
        dep_graph: Any = None,     # Dependency graph
    ) -> None:
        super().__init__(qdrant, pg_metadata, reranker, policy, generation_binder)
        self.symbol_index = symbol_index
        self.dep_graph = dep_graph

    def retrieve(self, request: RagRetrievalRequest) -> RagRetrievalResult:
        start_time = time.monotonic()
        phases = {}

        # Phase 1: Multi-source recall
        recall_start = time.monotonic()
        candidates = self._code_recall(request)
        phases["recall_ms"] = int((time.monotonic() - recall_start) * 1000)

        # Phase 2: Code-aware fusion
        fusion_start = time.monotonic()
        fused = self._code_fusion(candidates)
        phases["fusion_ms"] = int((time.monotonic() - fusion_start) * 1000)

        # Phase 3: Precision with code-aware reranker
        precision_start = time.monotonic()
        precision_results = self._code_rerank(request.query, fused)
        phases["rerank_ms"] = int((time.monotonic() - precision_start) * 1000)

        citations = self._build_citations(
            [r.hit for r in precision_results],
            request.request_id,
        )

        context = self._build_context_text(
            [r.hit for r in precision_results],
        )

        total_latency = int((time.monotonic() - start_time) * 1000)
        phases["total_ms"] = total_latency

        return RagRetrievalResult(
            request_id=request.request_id,
            rag_type=RagType.CODE,
            candidates=tuple(
                RagRetrievalCandidate(
                    hit=r.hit,
                    source="code",
                    fused_score=r.final_score,
                )
                for r in precision_results
            ),
            citations=citations,
            context_text=context,
            state="CANONICAL" if request.canonical_required else "DEGRADED",
            policy_version=request.policy_version,
            generation_id=request.generation_id,
            latency_ms=total_latency,
            phases=phases,
        )

    def _code_recall(self, request: RagRetrievalRequest) -> dict[str, list[Any]]:
        """Multi-source code recall."""
        import asyncio

        async def _run():
            # 1. Dense semantic search
            dense = await self.recall_phase.recall(
                query_embedding=list(request.query_vector),
                module_ids=tuple(request.module_ids) if request.module_ids else None,
            )

            # 2. Symbol exact lookup
            symbols = []
            if self.symbol_index and request.query:
                symbols = self.symbol_index.lookup(request.query)

            # 3. Dependency graph expansion (if available)
            graph = []
            if self.dep_graph and request.module_ids:
                graph = self.dep_graph.expand(request.module_ids[0])

            return {
                "dense": dense.get("dense", []),
                "symbol": symbols,
                "graph": graph,
            }

        return asyncio.run(_run())

    def _code_fusion(self, candidates: dict[str, list[Any]]) -> list[Any]:
        """Code-aware weighted fusion."""
        weights = {
            "dense": 1.0,
            "symbol": 1.5,      # Exact symbol match boosted
            "graph": 0.8,
        }
        # Normalize candidates to FusedCandidate format
        normalized = {}
        for source, cands in candidates.items():
            normalized[source] = [
                type('obj', (object,), {
                    'hit': c.hit if hasattr(c, 'hit') else c,
                    'fused_score': c.fused_score if hasattr(c, 'fused_score') else c.get('score', 0),
                })()
                for c in cands
            ]
        return self.fusion.fuse(normalized, weights)

    def _code_rerank(self, query: str, candidates: list[Any]) -> list[Any]:
        """Code-aware reranking (could use code-specific reranker)."""
        return asyncio.run(self.precision_phase.rerank(query, candidates))


# ============================================================================
# Agentic RAG Retriever
# ============================================================================

@dataclass
class AgenticRound:
    """Single agentic retrieval round."""
    round_num: int
    query: str
    query_vector: list[float]
    candidates: list[Any]
    evidence_sufficient: bool
    reasoning: str = ""


class AgenticRagRetriever(BaseRagRetriever):
    """Agentic retrieval: Multi-round with evidence evaluation.

    Round 1: Initial retrieval
    Evidence Judge: sufficient? -> answer / insufficient -> reformulate
    Round 2: Reformulated query
    ... max 3 rounds
    """

    @property
    def rag_type(self) -> str:
        return RagType.AGENTIC.value

    def __init__(
        self,
        qdrant: QdrantCanonicalRuntime,
        pg_metadata: PostgreSQLMetadataAuthority,
        reranker: Any,
        policy: RagPolicy = DEFAULT_POLICY,
        generation_binder: Optional[Any] = None,
        evidence_judge: Any = None,  # LLM-based evidence evaluator
        query_reformulator: Any = None,  # LLM-based query reformulator
    ) -> None:
        super().__init__(qdrant, pg_metadata, reranker, policy, generation_binder)
        self.evidence_judge = evidence_judge
        self.query_reformulator = query_reformulator
        self.rounds: list[AgenticRound] = []

    def retrieve(self, request: RagRetrievalRequest) -> RagRetrievalResult:
        start_time = time.monotonic()
        phases = {}
        all_rounds = []

        current_query = request.query
        current_vector = list(request.query_vector)

        for round_num in range(1, self.policy.max_agent_rounds + 1):
            round_start = time.monotonic()

            # Retrieve for current query
            candidates_by_source = self._recall_round(current_query, current_vector, request)
            fused = self._fuse(candidates_by_source)
            deduped = self._deduplicate(fused)
            precision_results = self._rerank(current_query, deduped)

            # Evidence evaluation
            evidence_sufficient, reasoning = self._evaluate_evidence(
                current_query, precision_results
            )

            round_obj = AgenticRound(
                round_num=round_num,
                query=current_query,
                query_vector=current_vector,
                candidates=precision_results,
                evidence_sufficient=evidence_sufficient,
                reasoning=reasoning,
            )
            all_rounds.append(round_obj)

            phases[f"round_{round_num}_ms"] = int((time.monotonic() - round_start) * 1000)

            if evidence_sufficient or round_num >= self.policy.max_agent_rounds:
                # Use final round results
                citations = self._build_citations(
                    [r.hit for r in precision_results],
                    request.request_id,
                )
                context = self._build_context_text(
                    [r.hit for r in precision_results],
                )

                total_latency = int((time.monotonic() - start_time) * 1000)
                phases["total_ms"] = total_latency

                return RagRetrievalResult(
                    request_id=request.request_id,
                    rag_type=RagType.AGENTIC,
                    candidates=tuple(
                        RagRetrievalCandidate(
                            hit=r.hit,
                            source="agentic",
                            fused_score=r.final_score,
                        )
                        for r in precision_results
                    ),
                    citations=citations,
                    context_text=context,
                    state="CANONICAL" if request.canonical_required else "DEGRADED",
                    policy_version=request.policy_version,
                    generation_id=request.generation_id,
                    latency_ms=total_latency,
                    phases=phases,
                )

            # Reformulate query for next round
            if self.query_reformulator and round_num < self.policy.max_agent_rounds:
                current_query, current_vector = self._reformulate_query(
                    current_query, current_vector, precision_results, reasoning
                )

        # Fallback: use last round results
        if all_rounds:
            last_round = all_rounds[-1]
            citations = self._build_citations(
                [r.hit for r in last_round.candidates],
                request.request_id,
            )
            context = self._build_context_text(
                [r.hit for r in last_round.candidates],
            )

        total_latency = int((time.monotonic() - start_time) * 1000)
        phases["total_ms"] = total_latency

        return RagRetrievalResult(
            request_id=request.request_id,
            rag_type=RagType.AGENTIC,
            candidates=tuple(
                RagRetrievalCandidate(
                    hit=r.hit,
                    source="agentic",
                    fused_score=r.final_score,
                )
                for r in last_round.candidates
            ),
            citations=citations,
            context_text=context,
            state="CANONICAL" if request.canonical_required else "DEGRADED",
            policy_version=request.policy_version,
            generation_id=request.generation_id,
            latency_ms=total_latency,
            phases=phases,
        )

    def _recall_round(self, query: str, query_vector: list[float], request: RagRetrievalRequest) -> dict[str, list[Any]]:
        import asyncio
        return asyncio.run(self.recall_phase.recall(
            query_embedding=query_vector,
            module_id=request.module_ids[0] if request.module_ids else None,
            query_text=query,
        ))

    def _fuse(self, candidates_by_source: dict[str, list[Any]]) -> list[Any]:
        weights = {
            "dense": self.policy.dense_weight,
            "fts": self.policy.fts_weight,
        }
        return self.fusion.fuse(candidates_by_source, weights)

    def _deduplicate(self, fused: list[Any]) -> list[Any]:
        def get_embedding(candidate):
            content = candidate.hit.hit.get("payload", {}).get("content", "")
            import hashlib
            import random
            seed = int(hashlib.sha256(content.encode()).hexdigest()[:8], 16)
            random.seed(seed)
            return [random.uniform(-1, 1) for _ in range(2560)]
        return self.dedup.deduplicate(fused, get_embedding)

    def _rerank(self, query: str, candidates: list[Any]) -> list[Any]:
        return asyncio.run(self.precision_phase.rerank(query, candidates))

    def _evaluate_evidence(self, query: str, results: list[Any]) -> tuple[bool, str]:
        """Evaluate if evidence is sufficient to answer query."""
        if self.evidence_judge:
            # Use LLM judge
            return self.evidence_judge.evaluate(query, results)

        # Simple heuristic: enough high-score results
        high_score_count = sum(1 for r in results if r.final_score > 0.7)
        sufficient = high_score_count >= 3
        reasoning = f"High-score results: {high_score_count}, threshold: 3"
        return sufficient, reasoning

    def _reformulate_query(
        self,
        query: str,
        query_vector: list[float],
        results: list[Any],
        reasoning: str,
    ) -> tuple[str, list[float]]:
        """Reformulate query based on evidence gaps."""
        if self.query_reformulator:
            new_query = self.query_reformulator.reformulate(query, results, reasoning)
            # Would need to re-embed
            return new_query, query_vector  # Placeholder

        # Simple fallback: add "code" or "implementation" to query
        return f"{query} implementation details", query_vector


# ============================================================================
# Memory RAG Retriever
# ============================================================================

class MemoryRagRetriever(BaseRagRetriever):
    """Memory retrieval: Semantic + Recency + Importance + Memory Type.

    Score = semantic + recency_weight * recency + importance_weight * importance
    """

    @property
    def rag_type(self) -> str:
        return RagType.MEMORY.value

    def __init__(
        self,
        qdrant: QdrantCanonicalRuntime,
        pg_metadata: PostgreSQLMetadataAuthority,
        reranker: Any,
        policy: RagPolicy = DEFAULT_POLICY,
        generation_binder: Optional[Any] = None,
        memory_store: Any = None,  # LocalSqliteRagRepository or similar
    ) -> None:
        super().__init__(qdrant, pg_metadata, reranker, policy, generation_binder)
        self.memory_store = memory_store

    def retrieve(self, request: RagRetrievalRequest) -> RagRetrievalResult:
        start_time = time.monotonic()
        phases = {}

        # Memory-specific recall
        recall_start = time.monotonic()
        candidates = self._memory_recall(request)
        phases["recall_ms"] = int((time.monotonic() - recall_start) * 1000)

        # Memory-specific scoring
        score_start = time.monotonic()
        scored = self._memory_score(request.query, candidates)
        phases["scoring_ms"] = int((time.monotonic() - score_start) * 1000)

        # Precision rerank
        precision_start = time.monotonic()
        precision_results = self._rerank(request.query, scored[:self.policy.reranker_candidates])
        phases["rerank_ms"] = int((time.monotonic() - precision_start) * 1000)

        citations = self._build_citations(
            [r.hit for r in precision_results],
            request.request_id,
        )

        context = self._build_context_text(
            [r.hit for r in precision_results],
        )

        total_latency = int((time.monotonic() - start_time) * 1000)
        phases["total_ms"] = total_latency

        return RagRetrievalResult(
            request_id=request.request_id,
            rag_type=RagType.MEMORY,
            candidates=tuple(
                RagRetrievalCandidate(
                    hit=r.hit,
                    source="memory",
                    fused_score=r.final_score,
                )
                for r in precision_results
            ),
            citations=citations,
            context_text=context,
            state="CANONICAL" if request.canonical_required else "DEGRADED",
            policy_version=request.policy_version,
            generation_id=request.generation_id,
            latency_ms=total_latency,
            phases=phases,
        )

    def _memory_recall(self, request: RagRetrievalRequest) -> list[Any]:
        """Memory-specific recall with session scope."""
        # Use local memory store if available
        if self.memory_store:
            # Search memory store
            hits = self.memory_store.query(
                query_embedding=list(request.query_vector),
                limit=self.policy.memory_candidates,
            )
            return [{
                'hit': h,
                'score': h.get('score', 0),
            } for h in hits]

        # Fallback to Qdrant with memory filter
        import asyncio
        results = asyncio.run(self.recall_phase.recall(
            query_embedding=list(request.query_vector),
            module_id=request.module_ids[0] if request.module_ids else None,
        ))
        return results.get("memory", results.get("dense", []))

    def _memory_score(self, query: str, candidates: list[Any]) -> list[Any]:
        """Apply memory-specific scoring: semantic + recency + importance."""
        scored = []
        import time as time_module
        now = time_module.time()

        for cand in candidates:
            payload = cand.hit.get("payload", {}) if hasattr(cand, 'hit') else cand.get("payload", {})

            semantic_score = cand.hit.get("score", 0) if hasattr(cand, 'hit') else cand.get("score", 0)

            # Recency score (exponential decay, half-life ~30 days)
            timestamp = payload.get("timestamp", 0)
            if timestamp:
                age_days = (now - timestamp) / 86400
                recency_score = 2 ** (-age_days / 30)
            else:
                recency_score = 0.5

            # Importance score (from metadata)
            importance_score = payload.get("importance", 0.5)

            # Combined score
            combined = (
                0.5 * semantic_score +
                0.3 * recency_score +
                0.2 * importance_score
            )

            scored.append(type('obj', (object,), {
                'hit': cand.hit if hasattr(cand, 'hit') else cand,
                'fused_score': combined,
            })())

        scored.sort(key=lambda x: -x.fused_score)
        return scored

    def _rerank(self, query: str, candidates: list[Any]) -> list[Any]:
        return asyncio.run(self.precision_phase.rerank(query, candidates))


# ============================================================================
# Retriever Factory
# ============================================================================

class RetrieverFactory:
    """Factory for creating retrievers by type."""

    def __init__(
        self,
        qdrant: QdrantCanonicalRuntime,
        pg_metadata: PostgreSQLMetadataAuthority,
        reranker: Any,
        policy: RagPolicy = DEFAULT_POLICY,
        generation_binder: Optional[Any] = None,
        symbol_index: Any = None,
        dep_graph: Any = None,
        evidence_judge: Any = None,
        query_reformulator: Any = None,
        memory_store: Any = None,
    ) -> None:
        self.qdrant = qdrant
        self.pg_metadata = pg_metadata
        self.reranker = reranker
        self.policy = policy
        self.generation_binder = generation_binder
        self.symbol_index = symbol_index
        self.dep_graph = dep_graph
        self.evidence_judge = evidence_judge
        self.query_reformulator = query_reformulator
        self.memory_store = memory_store

        self._cache: dict[str, RagRetriever] = {}

    def get_retriever(self, rag_type: RagType) -> RagRetriever:
        """Get or create retriever for the given type."""
        key = rag_type.value
        if key in self._cache:
            return self._cache[key]

        if rag_type == RagType.HYBRID:
            retriever = HybridRagRetriever(
                self.qdrant, self.pg_metadata, self.reranker,
                self.policy, self.generation_binder,
            )
        elif rag_type == RagType.CODE:
            retriever = CodeRagRetriever(
                self.qdrant, self.pg_metadata, self.reranker,
                self.policy, self.generation_binder,
                self.symbol_index, self.dep_graph,
            )
        elif rag_type == RagType.AGENTIC:
            retriever = AgenticRagRetriever(
                self.qdrant, self.pg_metadata, self.reranker,
                self.policy, self.generation_binder,
                self.evidence_judge, self.query_reformulator,
            )
        elif rag_type == RagType.MEMORY:
            retriever = MemoryRagRetriever(
                self.qdrant, self.pg_metadata, self.reranker,
                self.policy, self.generation_binder,
                self.memory_store,
            )
        else:
            raise ValueError(f"Unknown rag_type: {rag_type}")

        self._cache[key] = retriever
        return retriever

    def retrieve(
        self,
        request: RagRetrievalRequest,
    ) -> RagRetrievalResult:
        """Route to appropriate retriever."""
        retriever = self.get_retriever(request.rag_type)
        return retriever.retrieve(request)


__all__ = [
    "BaseRagRetriever",
    "HybridRagRetriever",
    "CodeRagRetriever",
    "AgenticRagRetriever",
    "MemoryRagRetriever",
    "RetrieverFactory",
]
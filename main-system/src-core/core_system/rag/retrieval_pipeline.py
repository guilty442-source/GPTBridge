"""Retrieval Pipeline — Recall/Precision 分相、Context Budget、Semantic Dedup。

固定三階段：
1. Recall Phase: Dense(30) + FTS(30) + Graph(optional) + Memory(optional) = ~120 candidates
2. Fusion Phase: RRF / weighted fusion → dedup
3. Precision Phase: Reranker(20) → Top-K(6)
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import numpy as np

from .generation_binder import VerifiedHit
from .citation import Citation, build_citation_from_hit

_logger = logging.getLogger("gptbridge.rag.pipeline")


class RetrievalPhase(str, Enum):
    RECALL = "RECALL"
    FUSION = "FUSION"
    PRECISION = "PRECISION"


@dataclass(frozen=True)
class RetrievalCandidate:
    """Candidate from recall phase."""
    hit: VerifiedHit
    source: str                    # "dense", "fts", "graph", "memory"
    score: float
    rank: int


@dataclass(frozen=True)
class FusedCandidate:
    """Candidate after fusion + dedup."""
    hit: VerifiedHit
    fused_score: float
    sources: list[str]
    original_scores: dict[str, float]


@dataclass(frozen=True)
class PrecisionResult:
    """Final precision-phase result."""
    hit: VerifiedHit
    reranker_score: float
    final_score: float
    citation: Citation


@dataclass
class RetrievalContext:
    """Context built from precision results with budget management."""
    results: list[PrecisionResult]
    total_tokens: int
    truncated: bool
    policy_version: str
    request_id: str


@dataclass(frozen=True)
class RagPolicy:
    """Immutable RAG policy object."""
    policy_id: str
    policy_version: str

    # Embedding
    embedding_model: str = "qwen3-embedding:4b"
    embedding_dimension: int = 2560

    # Chunking
    chunk_policy_version: str = "code-v1"
    chunk_size: int = 1200
    chunk_overlap: int = 200

    # Recall limits
    dense_candidates: int = 30
    fts_candidates: int = 30
    graph_candidates: int = 20
    memory_candidates: int = 10

    # Fusion
    rrf_k: int = 60
    fusion_method: str = "rrf"           # "rrf" | "weighted"
    dense_weight: float = 1.0
    fts_weight: float = 0.8
    graph_weight: float = 0.6
    memory_weight: float = 0.5

    # Dedup
    semantic_dedup_threshold: float = 0.95
    max_chunks_per_resource: int = 3

    # Precision
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_candidates: int = 20
    final_top_k: int = 6

    # Context budget
    max_context_tokens: int = 8000
    max_chunk_tokens: int = 1500

    # Agentic
    max_agent_rounds: int = 3
    max_queries_per_round: int = 3

    # Canonical
    canonical_required: bool = True
    fallback_allowed: bool = True


DEFAULT_POLICY = RagPolicy(
    policy_id="default",
    policy_version="v3",
)


class SemanticDeduplicator:
    """Semantic deduplication using embedding similarity."""

    def __init__(self, threshold: float = 0.95) -> None:
        self.threshold = threshold
        self._embeddings: dict[str, list[float]] = {}

    def deduplicate(
        self,
        candidates: list[FusedCandidate],
        get_embedding: callable,
    ) -> list[FusedCandidate]:
        """Remove near-duplicate candidates based on embedding similarity."""
        if not candidates:
            return []

        # Get embeddings for all candidates
        embeddings = []
        for c in candidates:
            key = c.hit.hit.get("point_id", c.hit.index_state.qdrant_point_id)
            if key in self._embeddings:
                emb = self._embeddings[key]
            else:
                content = c.hit.hit.get("payload", {}).get("content", "")
                emb = get_embedding(content)
                self._embeddings[key] = emb
            embeddings.append(np.array(emb))

        # Greedy dedup: keep first, remove similar
        kept = []
        removed_indices = set()

        for i, cand in enumerate(candidates):
            if i in removed_indices:
                continue
            kept.append(cand)
            # Compare with remaining
            for j in range(i + 1, len(candidates)):
                if j in removed_indices:
                    continue
                sim = self._cosine_similarity(embeddings[i], embeddings[j])
                if sim >= self.threshold:
                    removed_indices.add(j)
                    _logger.debug(
                        "SemanticDedup: removed duplicate (sim=%.3f) %s vs %s",
                        sim,
                        candidates[i].hit.index_state.resource_id,
                        candidates[j].hit.index_state.resource_id,
                    )

        _logger.info(
            "SemanticDedup: %d -> %d candidates (threshold=%.2f)",
            len(candidates), len(kept), self.threshold
        )
        return kept

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


class ContextBudgetManager:
    """Manages context token budget with per-resource limits."""

    def __init__(self, policy: RagPolicy) -> None:
        self.policy = policy

    def build_context(
        self,
        precision_results: list[PrecisionResult],
        request_id: str,
    ) -> RetrievalContext:
        """Build context respecting token budget and per-resource limits."""
        results = []
        total_tokens = 0
        resource_chunk_count: dict[str, int] = {}

        for pr in precision_results:
            resource_id = pr.hit.index_state.resource_id

            # Check per-resource limit
            if resource_chunk_count.get(resource_id, 0) >= self.policy.max_chunks_per_resource:
                _logger.debug(
                    "ContextBudget: skipping %s (max_chunks_per_resource=%d)",
                    resource_id, self.policy.max_chunks_per_resource
                )
                continue

            # Estimate tokens (rough: 1 token ≈ 4 chars)
            content = pr.hit.hit.get("payload", {}).get("content", "")
            chunk_tokens = min(len(content) // 4, self.policy.max_chunk_tokens)

            if total_tokens + chunk_tokens > self.policy.max_context_tokens:
                _logger.debug(
                    "ContextBudget: budget exceeded at %d tokens, stopping",
                    total_tokens
                )
                break

            results.append(pr)
            total_tokens += chunk_tokens
            resource_chunk_count[resource_id] = resource_chunk_count.get(resource_id, 0) + 1

        truncated = len(results) < len(precision_results)

        return RetrievalContext(
            results=results,
            total_tokens=total_tokens,
            truncated=truncated,
            policy_version=self.policy.policy_version,
            request_id=request_id,
        )


class ReciprocalRankFusion:
    """RRF fusion for multiple recall sources."""

    def __init__(self, k: int = 60) -> None:
        self.k = k

    def fuse(
        self,
        candidates_by_source: dict[str, list[RetrievalCandidate]],
        weights: Optional[dict[str, float]] = None,
    ) -> list[FusedCandidate]:
        """Fuse candidates from multiple sources using RRF."""
        if not candidates_by_source:
            return []

        weights = weights or {}
        score_map: dict[str, float] = {}
        source_map: dict[str, list[str]] = {}
        score_detail_map: dict[str, dict[str, float]] = {}

        for source, candidates in candidates_by_source.items():
            weight = weights.get(source, 1.0)
            for rank, cand in enumerate(candidates):
                point_id = cand.hit.hit.get("point_id", cand.hit.index_state.qdrant_point_id)
                rrf_score = weight / (self.k + rank + 1)

                if point_id not in score_map:
                    score_map[point_id] = 0.0
                    source_map[point_id] = []
                    score_detail_map[point_id] = {}

                score_map[point_id] += rrf_score
                source_map[point_id].append(source)
                score_detail_map[point_id][source] = score_detail_map[point_id].get(source, 0) + rrf_score

        # Build fused candidates
        fused = []
        for point_id, fused_score in sorted(score_map.items(), key=lambda x: -x[1]):
            # Find the original candidate (first occurrence)
            original = None
            for src, cands in candidates_by_source.items():
                for c in cands:
                    pid = c.hit.hit.get("point_id", c.hit.index_state.qdrant_point_id)
                    if pid == point_id:
                        original = c.hit
                        break
                if original:
                    break

            if original:
                fused.append(FusedCandidate(
                    hit=original,
                    fused_score=fused_score,
                    sources=source_map[point_id],
                    original_scores=score_detail_map[point_id],
                ))

        return fused


class RecallPhase:
    """Phase 1: Multi-source recall."""

    def __init__(
        self,
        qdrant_runtime: Any,
        pg_metadata: Any,
        policy: RagPolicy,
    ) -> None:
        self.qdrant = qdrant_runtime
        self.pg = pg_metadata
        self.policy = policy

    async def recall(
        self,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        query_text: Optional[str] = None,
        module_ids: Optional[tuple[str, ...]] = None,
    ) -> dict[str, list[RetrievalCandidate]]:
        """Run recall from all sources."""
        results = {}

        # Dense retrieval
        dense_hits = await self.qdrant.search(
            query_vector=query_embedding,
            module_id=module_id,
            module_ids=module_ids,
            top_k=self.policy.dense_candidates,
        )
        results["dense"] = [
            RetrievalCandidate(
                hit=VerifiedHit(hit=h, index_state=None, verification="VERIFIED", metadata={}),
                source="dense",
                score=h.get("score", 0.0),
                rank=i,
            )
            for i, h in enumerate(dense_hits)
        ]

        # FTS retrieval (if query_text provided)
        if query_text:
            fts_hits = await self._fts_search(query_text, module_id)
            results["fts"] = [
                RetrievalCandidate(
                    hit=VerifiedHit(hit=h, index_state=None, verification="VERIFIED", metadata={}),
                    source="fts",
                    score=h.get("score", 0.0),
                    rank=i,
                )
                for i, h in enumerate(fts_hits)
            ]

        # Graph retrieval (placeholder)
        # Memory retrieval (placeholder)

        return results

    async def _fts_search(
        self,
        query_text: str,
        module_id: Optional[str],
    ) -> list[dict[str, Any]]:
        """Full-text search via PostgreSQL."""
        try:
            return await self.pg.full_text_search(
                query=query_text,
                module_id=module_id,
                limit=self.policy.fts_candidates,
            )
        except Exception as e:
            _logger.warning("RecallPhase: FTS search failed: %s", e)
            return []


class PrecisionPhase:
    """Phase 3: Reranking + final selection."""

    def __init__(
        self,
        reranker: Any,  # CrossEncoder or similar
        policy: RagPolicy,
    ) -> None:
        self.reranker = reranker
        self.policy = policy

    async def rerank(
        self,
        query: str,
        fused_candidates: list[FusedCandidate],
    ) -> list[PrecisionResult]:
        """Rerank fused candidates and return top-K."""
        if not fused_candidates:
            return []

        # Prepare pairs for reranker
        pairs = [(query, c.hit.hit.get("payload", {}).get("content", "")) for c in fused_candidates[:self.policy.reranker_candidates]]

        # Rerank
        try:
            scores = self.reranker.predict(pairs)
        except Exception as e:
            _logger.warning("PrecisionPhase: reranker failed, using fused scores: %s", e)
            scores = [c.fused_score for c in fused_candidates[:self.policy.reranker_candidates]]

        # Build results
        results = []
        for i, (cand, rerank_score) in enumerate(zip(fused_candidates[:self.policy.reranker_candidates], scores)):
            # Build citation
            citation = build_citation_from_hit(
                hit=cand.hit.hit,
                index_state=cand.hit.index_state,
                citation_id=f"R{i+1}",
                module_id=cand.hit.index_state.module_id,
            )

            # Final score: blend reranker + fused
            final_score = 0.7 * rerank_score + 0.3 * cand.fused_score

            results.append(PrecisionResult(
                hit=cand.hit,
                reranker_score=rerank_score,
                final_score=final_score,
                citation=citation,
            ))

        # Sort by final score and take top-K
        results.sort(key=lambda x: -x.final_score)
        return results[:self.policy.final_top_k]


class RetrievalPipeline:
    """Complete retrieval pipeline: Recall → Fusion → Precision."""

    def __init__(
        self,
        qdrant_runtime: Any,
        pg_metadata: Any,
        reranker: Any,
        policy: Optional[RagPolicy] = None,
    ) -> None:
        self.policy = policy or DEFAULT_POLICY
        self.recall_phase = RecallPhase(qdrant_runtime, pg_metadata, self.policy)
        self.fusion = ReciprocalRankFusion(k=self.policy.rrf_k)
        self.dedup = SemanticDeduplicator(threshold=self.policy.semantic_dedup_threshold)
        self.precision_phase = PrecisionPhase(reranker, self.policy)
        self.context_budget = ContextBudgetManager(self.policy)

    async def retrieve(
        self,
        query: str,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> RetrievalContext:
        """Run full retrieval pipeline."""
        req_id = request_id or str(uuid.uuid4())[:8]
        start = time.monotonic()

        # Phase 1: Recall
        recall_start = time.monotonic()
        candidates_by_source = await self.recall_phase.recall(
            query_embedding=query_embedding,
            module_id=module_id,
            query_text=query,
        )
        recall_time = time.monotonic() - recall_start

        # Phase 2: Fusion
        fusion_start = time.monotonic()
        weights = {
            "dense": self.policy.dense_weight,
            "fts": self.policy.fts_weight,
            "graph": self.policy.graph_weight,
            "memory": self.policy.memory_weight,
        }
        fused = self.fusion.fuse(candidates_by_source, weights)
        fused_time = time.monotonic() - fusion_start

        # Phase 2b: Dedup
        dedup_start = time.monotonic()
        deduped = self.dedup.deduplicate(
            fused,
            get_embedding=lambda c: self._get_embedding(c),  # Placeholder
        )
        dedup_time = time.monotonic() - dedup_start

        # Phase 3: Precision
        precision_start = time.monotonic()
        precision_results = await self.precision_phase.rerank(query, deduped)
        precision_time = time.monotonic() - precision_start

        # Context budget
        context = self.context_budget.build_context(precision_results, req_id)

        total_time = time.monotonic() - start
        _logger.info(
            "RetrievalPipeline[%s]: recall=%.0fms fusion=%.0fms dedup=%.0fms precision=%.0fms total=%.0fms results=%d",
            req_id,
            recall_time * 1000, fused_time * 1000, dedup_time * 1000,
            precision_time * 1000, total_time * 1000, len(context.results)
        )

        return context

    def _get_embedding(self, content: str) -> list[float]:
        """Placeholder for embedding function."""
        import hashlib
        seed = int(hashlib.sha256(content.encode()).hexdigest()[:8], 16)
        import random
        random.seed(seed)
        return [random.uniform(-1, 1) for _ in range(2560)]


__all__ = [
    "RetrievalPhase",
    "RetrievalCandidate",
    "FusedCandidate",
    "PrecisionResult",
    "RetrievalContext",
    "RagPolicy",
    "DEFAULT_POLICY",
    "SemanticDeduplicator",
    "ContextBudgetManager",
    "ReciprocalRankFusion",
    "RecallPhase",
    "PrecisionPhase",
    "RetrievalPipeline",
]
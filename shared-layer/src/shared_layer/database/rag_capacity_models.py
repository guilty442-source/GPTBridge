"""RAG capacity policy models: queues, budgets, profiles (migration 088).

Pure data — no database access lives here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ============================================================================
# Enums / constants
# ============================================================================

class QueueState(str, Enum):
    ACCEPTING = "ACCEPTING"
    THROTTLED = "THROTTLED"
    PAUSED = "PAUSED"
    DRAINING = "DRAINING"


class QueueDecision(str, Enum):
    ACCEPT = "ACCEPT"
    INDEX_PENDING = "INDEX_PENDING"   # accept metadata, defer indexing
    DEFER = "DEFER"
    REJECT = "REJECT"


QUEUE_NAMES: tuple[str, ...] = (
    "ingestion",
    "query_embedding",
    "index_embedding",
    "reranker",
    "index_runner",
    "reconcile",
    "rebuild",
)

QUERY_EMBEDDING_QUEUE = "query_embedding"
INDEX_EMBEDDING_QUEUE = "index_embedding"

_INTERACTIVE_OPERATIONS: frozenset[str] = frozenset({
    "query_embedding", "reranker", "generation", "retrieval",
})
_BACKGROUND_OPERATIONS: frozenset[str] = frozenset({
    "document_embedding", "index_embedding", "index_runner",
    "reconcile", "rebuild",
})


def pool_for_operation(operation: str) -> str:
    """Interactive vs Background pool for a RAG operation (spec separation)."""
    if operation in _INTERACTIVE_OPERATIONS:
        return "interactive"
    if operation in _BACKGROUND_OPERATIONS:
        return "background"
    return "background"


class AgenticStop(str, Enum):
    EVIDENCE_SUFFICIENT = "EVIDENCE_SUFFICIENT"
    MAX_ROUNDS = "MAX_ROUNDS"
    TIME_BUDGET = "TIME_BUDGET"
    NO_NEW_EVIDENCE = "NO_NEW_EVIDENCE"


# ============================================================================
# Capacity policy dataclasses (budgets, one per layer)
# ============================================================================

@dataclass(frozen=True)
class IngestionBudget:
    max_files_per_job: int = 64
    max_bytes_per_file: int = 40 * 1024 * 1024
    max_chunks_per_resource: int = 512
    max_chunks_per_batch: int = 32
    max_pending_index_jobs: int = 500


@dataclass(frozen=True)
class EmbeddingBudget:
    batch_size: int = 8
    max_parallel_batches: int = 4
    max_queue_depth: int = 512
    timeout_ms: int = 30_000


@dataclass(frozen=True)
class RetrievalBudget:
    dense_candidate_limit: int = 30
    sparse_candidate_limit: int = 30
    graph_candidate_limit: int = 20
    memory_candidate_limit: int = 10


@dataclass(frozen=True)
class RerankerBudget:
    max_candidates: int = 40
    batch_size: int = 16
    timeout_ms: int = 20_000


@dataclass(frozen=True)
class GenerationBudget:
    max_context_tokens: int = 8_000
    max_chunks: int = 20
    max_chunks_per_resource: int = 3
    generation_timeout_ms: int = 120_000


@dataclass(frozen=True)
class VectorIndexProfile:
    """HNSW profile presets — small/medium/large chosen by benchmark, not fiat."""
    name: str
    m: int = 16
    ef_construct: int = 100
    max_points: int = 100_000

    def params(self) -> dict[str, int]:
        return {"m": self.m, "ef_construct": self.ef_construct}


@dataclass(frozen=True)
class CachePolicy:
    """Query caches: never the final LLM answer by default."""
    embedding_ttl_seconds: int = 86_400
    retrieval_ttl_seconds: int = 60
    reranker_results_ttl_seconds: int = 60
    cache_final_answer: bool = False


@dataclass(frozen=True)
class TierDefaults:
    """Hot / Warm / Cold index tiers."""
    hot_ttl_seconds: Optional[int] = 2_592_000
    warm_ttl_seconds: Optional[int] = 7_776_000
    cold_ttl_seconds: Optional[int] = None
    cold_search_by_default: bool = False


@dataclass(frozen=True)
class RagCapacityPolicy:
    """The full per-layer capacity policy (mirrors the seeded v1 row)."""
    policy_version: str = "rag-capacity-v1"
    ingestion: IngestionBudget = field(default_factory=IngestionBudget)
    embedding: EmbeddingBudget = field(default_factory=EmbeddingBudget)
    retrieval: RetrievalBudget = field(default_factory=RetrievalBudget)
    reranker: RerankerBudget = field(default_factory=RerankerBudget)
    generation: GenerationBudget = field(default_factory=GenerationBudget)
    vector_profiles: tuple[VectorIndexProfile, ...] = field(default_factory=lambda: (
        VectorIndexProfile("small", m=16, ef_construct=100, max_points=100_000),
        VectorIndexProfile("medium", m=32, ef_construct=200, max_points=1_000_000),
        VectorIndexProfile("large", m=64, ef_construct=400, max_points=10_000_000),
    ))
    cache: CachePolicy = field(default_factory=CachePolicy)
    tier_defaults: TierDefaults = field(default_factory=TierDefaults)

    # -- serialization ------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "ingestion": _asdict(self.ingestion),
            "embedding": _asdict(self.embedding),
            "retrieval": _asdict(self.retrieval),
            "reranker": _asdict(self.reranker),
            "generation": _asdict(self.generation),
            "vector_profiles": {
                p.name: _asdict(p) for p in self.vector_profiles
            },
            "cache": _asdict(self.cache),
            "tier_defaults": _asdict(self.tier_defaults),
        }

    @classmethod
    def from_json(cls, doc: dict[str, Any]) -> "RagCapacityPolicy":
        ingest = IngestionBudget(**{**_asdict(IngestionBudget()), **(doc.get("ingestion") or {})})
        embed = EmbeddingBudget(**{**_asdict(EmbeddingBudget()), **(doc.get("embedding") or {})})
        retr = RetrievalBudget(**{**_asdict(RetrievalBudget()), **(doc.get("retrieval") or {})})
        rerank = RerankerBudget(**{**_asdict(RerankerBudget()), **(doc.get("reranker") or {})})
        gen = GenerationBudget(**{**_asdict(GenerationBudget()), **(doc.get("generation") or {})})
        profiles = tuple(
            VectorIndexProfile(name=k, **{k2: v for k2, v in p.items() if k2 != "name"})
            for k, p in (doc.get("vector_profiles") or {}).items()
        ) or tuple([
            p for p in RagCapacityPolicy.__dataclass_fields__["vector_profiles"].default
        ])
        return cls(
            policy_version=str(doc.get("policy_version") or "rag-capacity-v1"),
            ingestion=ingest,
            embedding=embed,
            retrieval=retr,
            reranker=rerank,
            generation=gen,
            vector_profiles=profiles,
            cache=CachePolicy(**{**_asdict(CachePolicy()), **(doc.get("cache") or {})}),
            tier_defaults=TierDefaults(
                **{**{k: v for k, v in _asdict(TierDefaults()).items()}, **(doc.get("tier_defaults") or {})}
            ),
        )


def _asdict(obj: Any) -> dict[str, Any]:
    from dataclasses import asdict
    return asdict(obj)


DEFAULT_CAPACITY_POLICY = RagCapacityPolicy()

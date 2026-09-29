"""RAG Capacity Governance facade (migration 088).

Every layer of the RAG pipeline — ingestion, embedding, retrieval, reranker,
generation — carries an explicit upper bound instead of a single shared knob.
This is the CONTROL plane for that contract:

    RagCapacityPolicy     — per-layer budgets (u+ defaults for a local rig)
    QueueState/admission  — bounded backpressure (ACCEPTING / THROTTLED /
                            PAUSED / DRAINING) + INDEX_PENDING decision so a
                            large reindex never timeouts ingestion or blocks
                            queries.
    generation registry   — logical collection <-> physical generation with
                            BUILDING -> VERIFYING -> ACTIVE (alias swap).
    traces / latency      — agentic retrieval traces (round + stop_reason) and
                            per-phase timings for the RAG SLOs.

Separation of pools (spec): query-facing work (query embedding, reranker,
generation) is interactive; background work (document embedding, index
runner, reconcile, rebuild) is background.  Priority is query > reconcile >
bulk reindex — the adaptive admission ladder already maps these classes.

Pure helpers live here (no database) so the pipeline can adopt them
immediately: reranker batching, context diversity, adjacent-chunk merge,
no-new-evidence stop, vector payload-filter construction, index-profile
selection, and the two cache kinds (embedding vs retrieval).

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist; deny-by-default.
"""
from __future__ import annotations


from .rag_capacity_cache import EmbeddingCache, RetrievalCache
from .rag_capacity_helpers import (
    LIFECYCLE_OPERATIONS,
    LIFECYCLE_QUEUE,
    LifecycleGateDecision,
    admit,
    agentic_stop_reason,
    build_vector_filter,
    embedding_cache_key,
    enforce_diversity,
    gate_lifecycle_operation,
    has_new_evidence,
    merge_adjacent_chunks,
    reranker_batches,
    retrieval_cache_key,
    select_index_profile,
)
from .rag_capacity_models import (
    INDEX_EMBEDDING_QUEUE,
    QUERY_EMBEDDING_QUEUE,
    QUEUE_NAMES,
    AgenticStop,
    CachePolicy,
    DEFAULT_CAPACITY_POLICY,
    EmbeddingBudget,
    GenerationBudget,
    IngestionBudget,
    QueueDecision,
    QueueState,
    RagCapacityPolicy,
    RerankerBudget,
    RetrievalBudget,
    TierDefaults,
    VectorIndexProfile,
    pool_for_operation,
)
from .rag_capacity_store import (
    advance_generation,
    current_capacity_policy,
    initialize_generation,
    list_generations,
    list_phase_latency,
    list_queue_states,
    list_rag_slo,
    list_retrieval_traces,
    queue_admission,
    record_phase_latency,
    record_retrieval_trace,
    set_queue_state,
    upsert_capacity_policy,
)


__all__ = [
    "QueueState",
    "QueueDecision",
    "AgenticStop",
    "QUEUE_NAMES",
    "QUERY_EMBEDDING_QUEUE",
    "INDEX_EMBEDDING_QUEUE",
    "pool_for_operation",
    "IngestionBudget",
    "EmbeddingBudget",
    "RetrievalBudget",
    "RerankerBudget",
    "GenerationBudget",
    "VectorIndexProfile",
    "CachePolicy",
    "TierDefaults",
    "RagCapacityPolicy",
    "DEFAULT_CAPACITY_POLICY",
    "current_capacity_policy",
    "upsert_capacity_policy",
    "set_queue_state",
    "queue_admission",
    "record_phase_latency",
    "record_retrieval_trace",
    "initialize_generation",
    "advance_generation",
    "list_rag_slo",
    "admit",
    "LIFECYCLE_OPERATIONS",
    "LIFECYCLE_QUEUE",
    "LifecycleGateDecision",
    "gate_lifecycle_operation",
    "build_vector_filter",
    "select_index_profile",
    "reranker_batches",
    "enforce_diversity",
    "merge_adjacent_chunks",
    "has_new_evidence",
    "agentic_stop_reason",
    "retrieval_cache_key",
    "embedding_cache_key",
    "EmbeddingCache",
    "RetrievalCache",
]
"""RAG Typed Contracts — 所有請求/結果/實體的不可變資料契約。

取代鬆散 dict，確保編譯期類型安全與序列化一致性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4


# ============================================================================
# Enums
# ============================================================================

class RagType(str, Enum):
    """RAG architecture types."""
    HYBRID = "hybrid"
    CODE = "code"
    AGENTIC = "agentic"
    MEMORY = "memory"


class KnowledgeKind(str, Enum):
    """Knowledge classification."""
    SOURCE = "SOURCE"
    DERIVED = "DERIVED"
    MEMORY = "MEMORY"
    GENERATED = "GENERATED"


class DataCategory(str, Enum):
    """Data category for access control."""
    PUBLIC = "public"
    INTERNAL = "internal"
    PRIVATE = "private"
    RESTRICTED = "restricted"


class ResourceType(str, Enum):
    """Resource type."""
    DOCUMENT = "document"
    CODE = "code"
    SYMBOL = "symbol"
    MEMORY = "memory"
    AGENT_STATE = "agent_state"


class LifecycleState(str, Enum):
    """Resource lifecycle states."""
    SOURCE = "SOURCE"
    PARSED = "PARSED"
    CHUNKED = "CHUNKED"
    EMBEDDED = "EMBEDDED"
    INDEX_PENDING = "INDEX_PENDING"
    CANONICAL_INDEXED = "CANONICAL_INDEXED"
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    TOMBSTONED = "TOMBSTONED"
    DEGRADED_PENDING = "DEGRADED_PENDING"
    RECONCILING = "RECONCILING"


class BackendType(str, Enum):
    """Backend implementation type."""
    CANONICAL = "canonical"
    DEGRADED = "degraded"


class OutboxState(str, Enum):
    """Outbox event states."""
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    RETRY = "RETRY"
    DEAD_LETTER = "DEAD_LETTER"


class GenerationState(str, Enum):
    """Index generation states."""
    BUILDING = "BUILDING"
    VERIFYING = "VERIFYING"
    VALIDATING = "VERIFYING"   # takeover spec name — same lifecycle step
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    FAILED = "FAILED"


class OutboxOperation(str, Enum):
    """Canonical outbox operations (gptbridge_rag.outbox_event).

    The ``*_RESOURCE`` names are the canonical Phase-2 vocabulary; the
    bare ``UPSERT``/``DELETE``/``REINDEX`` spellings are accepted legacy
    wire values written by pre-Phase-2 producers.
    """
    UPSERT_RESOURCE = "UPSERT_RESOURCE"
    DELETE_RESOURCE = "DELETE_RESOURCE"
    REINDEX_RESOURCE = "REINDEX_RESOURCE"
    RECONCILE_RESOURCE = "RECONCILE_RESOURCE"
    UPDATE_METADATA = "UPDATE_METADATA"
    UPSERT = "UPSERT"
    DELETE = "DELETE"
    REINDEX = "REINDEX"


class RetrievalPhase(str, Enum):
    """Retrieval pipeline phases."""
    RECALL = "RECALL"
    FUSION = "FUSION"
    PRECISION = "PRECISION"


# ============================================================================
# Core Identifiers
# ============================================================================

def generate_request_id() -> str:
    return f"rag-{uuid4().hex[:12]}"

def generate_event_id() -> str:
    return str(uuid4())

def generate_point_id() -> str:
    return str(uuid4())


# ============================================================================
# Index Contracts
# ============================================================================

@dataclass(frozen=True, slots=True)
class RagChunk:
    """Stable chunk definition - content NOT serialized to Qdrant."""
    chunk_id: str
    locator_id: str
    sequence: int
    character_start: int
    character_end: int
    content_hash: str
    content: str = ""  # Only in application memory, never serialized to vector store


@dataclass(frozen=True, slots=True)
class RagIndexRequest:
    """Canonical index request - all fields required."""
    request_id: str
    module_id: str
    resource_id: str
    resource_type: ResourceType
    data_category: DataCategory

    content_hash: str
    source_version: int

    rag_types: tuple[RagType, ...]

    chunks: tuple[RagChunk, ...]

    embedding_model: str
    embedding_dimension: int

    generation_id: str
    policy_version: str

    knowledge_kind: KnowledgeKind = KnowledgeKind.SOURCE


@dataclass(frozen=True, slots=True)
class RagIndexResult:
    """Index operation result."""
    request_id: str
    resource_id: str
    module_id: str
    success: bool
    new_version: int
    state: LifecycleState
    qdrant_point_ids: tuple[str, ...]
    error_message: Optional[str] = None
    from_cache: bool = False


@dataclass(frozen=True, slots=True)
class RagDeleteRequest:
    """Delete request."""
    request_id: str
    module_id: str
    resource_id: str
    generation_id: str
    reason: str = "user_deleted"


@dataclass(frozen=True, slots=True)
class RagDeleteResult:
    """Delete operation result."""
    request_id: str
    resource_id: str
    module_id: str
    success: bool
    state: LifecycleState
    error_message: Optional[str] = None


@dataclass(frozen=True, slots=True)
class RagSearchRequest:
    """Search request."""
    request_id: str
    query_vector: tuple[float, ...]
    module_ids: tuple[str, ...]
    top_k: int
    score_threshold: float
    generation_id: Optional[str] = None
    filter: Optional[dict[str, Any]] = None


@dataclass(frozen=True, slots=True)
class RagSearchHit:
    """Single search hit with verification metadata."""
    point_id: str
    score: float
    locator_id: str
    chunk_id: str
    resource_id: str
    module_id: str
    generation_id: str
    source_version: int
    content_hash: str
    verified: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RagSearchResult:
    """Search results."""
    request_id: str
    hits: tuple[RagSearchHit, ...]
    total_candidates: int
    verified_count: int
    dropped_count: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class RagReconcileRequest:
    """Reconciliation request."""
    request_id: str
    generation_id: str
    resource_ids: Optional[tuple[str, ...]] = None
    module_ids: Optional[tuple[str, ...]] = None
    full_scan: bool = False


@dataclass(frozen=True, slots=True)
class RagReconcileResult:
    """Reconciliation result."""
    request_id: str
    checked: int
    fixed: int
    failed: int
    discrepancies: list[dict[str, Any]]
    latency_ms: int


@dataclass(frozen=True, slots=True)
class RagBackendHealth:
    """Backend health status."""
    backend_type: BackendType
    healthy: bool
    state: str
    components: dict[str, bool]
    active_generation: Optional[str] = None
    alias_target: Optional[str] = None
    outbox_pending: int = 0
    reconciliation_pending: int = 0
    embedding_available: bool = True
    metadata_available: bool = True
    vector_available: bool = True
    message: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ============================================================================
# Retrieval Contracts
# ============================================================================

@dataclass(frozen=True, slots=True)
class RagRetrievalRequest:
    """Unified retrieval request."""
    request_id: str
    query: str
    query_vector: tuple[float, ...]
    module_ids: tuple[str, ...]
    rag_type: RagType
    generation_id: str
    policy_version: str

    candidate_limit: int
    reranker_limit: int
    top_k: int
    max_context_tokens: int

    canonical_required: bool
    fallback_allowed: bool

    # Optional context
    session_id: Optional[str] = None
    user_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class RagRetrievalCandidate:
    """Candidate from recall phase."""
    hit: RagSearchHit
    source: str  # "dense", "fts", "graph", "memory"
    fused_score: float = 0.0


@dataclass(frozen=True, slots=True)
class RagRetrievalResult:
    """Retrieval result with citations."""
    request_id: str
    rag_type: RagType
    candidates: tuple[RagRetrievalCandidate, ...]
    citations: tuple["Citation", ...]
    context_text: str
    state: str  # BackendType.CANONICAL or DEGRADED
    policy_version: str
    generation_id: str
    latency_ms: int
    phases: dict[str, int] = field(default_factory=dict)  # phase -> ms


# ============================================================================
# Canonical Vector Point Contract
# ============================================================================

@dataclass(frozen=True, slots=True)
class CanonicalVectorPoint:
    """Qdrant point contract - NO content/text/path fields."""
    point_id: str
    vector: tuple[float, ...]

    # Identifiers
    locator_id: str
    chunk_id: str
    resource_id: str
    module_id: str

    # Classification
    rag_types: tuple[RagType, ...]
    data_category: DataCategory
    resource_type: ResourceType

    # Versioning
    generation_id: str
    source_version: int
    content_hash: str
    embedding_model: str

    # Forbidden fields (by design NOT present):
    # content, text, path, physical_location, windows_path, raw_content


# ============================================================================
# Query Plan
# ============================================================================

@dataclass(frozen=True, slots=True)
class RagQueryPlan:
    """Executable query plan - fully auditable."""
    request_id: str

    # Architecture
    rag_architecture: RagType
    generation_mode: str

    # Scope
    module_ids: tuple[str, ...]
    data_categories: tuple[DataCategory, ...]

    # Limits
    candidate_limit: int
    reranker_limit: int
    top_k: int
    max_context_tokens: int

    # Versioning
    generation_id: str
    policy_version: str

    # Canonical
    canonical_required: bool
    fallback_allowed: bool

    # Routing
    routing_decision: dict[str, Any] = field(default_factory=dict)

    @property
    def is_canonical(self) -> bool:
        return self.canonical_required


# ============================================================================
# Citation
# ============================================================================

@dataclass(frozen=True, slots=True)
class Citation:
    """Verifiable citation for LLM responses."""
    citation_id: str
    resource_id: str
    chunk_id: str
    locator_id: str
    character_start: int
    character_end: int
    content_hash: str
    generation_id: str
    retrieval_score: float
    reranker_score: Optional[float] = None
    module_id: Optional[str] = None


# ============================================================================
# Generation Contracts
# ============================================================================

@dataclass(frozen=True, slots=True)
class IndexGeneration:
    """Index generation metadata."""
    generation_id: str
    index_schema_version: str
    embedding_model: str
    embedding_dimension: int
    chunk_policy_version: str
    chunk_size: int
    chunk_overlap: int
    created_at: str
    state: GenerationState
    collection_name: str
    alias_name: str
    previous_generation_id: Optional[str] = None
    points_count: int = 0
    verification_result: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    """Generation management configuration."""
    alias_name: str = "gptbridge_shared_knowledge"
    index_schema_version: str = "v3"
    embedding_model: str = "qwen3-embedding:4b"
    embedding_dimension: int = 2560
    chunk_policy_version: str = "v3"
    chunk_size: int = 1200
    chunk_overlap: int = 200
    max_generations_to_keep: int = 3


# ============================================================================
# Embedding Cache
# ============================================================================

@dataclass(frozen=True, slots=True)
class EmbeddingCacheKey:
    """Embedding cache lookup key."""
    content_hash: str
    embedding_model: str
    embedding_dimension: int

    def __str__(self) -> str:
        return f"{self.content_hash}:{self.embedding_model}:{self.embedding_dimension}"


@dataclass(frozen=True, slots=True)
class EmbeddingCacheEntry:
    """Embedding cache entry."""
    cache_id: str
    content_hash: str
    embedding_model: str
    embedding_dimension: int
    vector: tuple[float, ...]
    created_at: str
    hit_count: int = 0


# ============================================================================
# Outbox Event
# ============================================================================

@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """Outbox event for transactional indexing."""
    event_id: str
    request_id: str
    operation: str  # "UPSERT", "DELETE", "REINDEX", "UPDATE_METADATA"
    module_id: str
    resource_id: str
    generation_id: str
    source_version: int
    content_hash: str
    payload: Optional[dict[str, Any]] = None
    state: OutboxState = OutboxState.PENDING
    attempt_count: int = 0
    next_retry_at: Optional[str] = None
    last_error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None


# ============================================================================
# Provenance
# ============================================================================

@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    """Derived knowledge provenance."""
    derived_resource_id: str
    derived_chunk_id: str
    source_resource_id: str
    source_chunk_id: str
    source_generation_id: str
    generation_model: str
    generated_at: str
    content_hash: str


# ============================================================================
# Policy
# ============================================================================

@dataclass(frozen=True, slots=True)
class RagPolicy:
    """Immutable RAG policy."""
    policy_id: str
    policy_version: str

    dag_enabled: bool = True
    cag_enabled: bool = True
    cag_embedding_cache_enabled: bool = True
    cag_evidence_cache_enabled: bool = True
    cag_context_cache_enabled: bool = True
    cag_answer_cache_enabled: bool = False
    cag_ttl_seconds: int = 600
    cag_max_entries: int = 4096
    max_dag_nodes: int = 64
    max_parallel_nodes: int = 4

    embedding_model: str = "qwen3-embedding:4b"
    embedding_dimension: int = 2560

    chunk_policy_version: str = "code-v1"
    chunk_size: int = 1200
    chunk_overlap: int = 200

    dense_candidates: int = 30
    fts_candidates: int = 30
    graph_candidates: int = 20
    memory_candidates: int = 10

    rrf_k: int = 60
    fusion_method: str = "rrf"
    dense_weight: float = 1.0
    fts_weight: float = 0.8
    graph_weight: float = 0.6
    memory_weight: float = 0.5

    semantic_dedup_threshold: float = 0.95
    max_chunks_per_resource: int = 3

    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    reranker_candidates: int = 20
    final_top_k: int = 6

    max_context_tokens: int = 8000
    max_chunk_tokens: int = 1500

    max_agent_rounds: int = 3
    max_queries_per_round: int = 3

    canonical_required: bool = True
    fallback_allowed: bool = True


DEFAULT_POLICY = RagPolicy(
    policy_id="default",
    policy_version="v3",
)


# ============================================================================
# Model Registry
# ============================================================================

@dataclass(frozen=True, slots=True)
class RagModelRegistration:
    """Model registry entry."""
    role: str  # "embedding", "reranker", "generation"
    model_id: str
    runtime: str  # "ollama", "transformers", "vllm", etc.
    revision: str
    dimension: Optional[int]
    enabled: bool
    canonical: bool
    installed: bool
    verified_at: Optional[str] = None


__all__ = [
    # Enums
    "RagType",
    "KnowledgeKind",
    "DataCategory",
    "ResourceType",
    "LifecycleState",
    "BackendType",
    "OutboxState",
    "GenerationState",
    "RetrievalPhase",
    # Core IDs
    "generate_request_id",
    "generate_event_id",
    "generate_point_id",
    # Index Contracts
    "RagChunk",
    "RagIndexRequest",
    "RagIndexResult",
    "RagDeleteRequest",
    "RagDeleteResult",
    "RagSearchRequest",
    "RagSearchHit",
    "RagSearchResult",
    "RagReconcileRequest",
    "RagReconcileResult",
    "RagBackendHealth",
    # Retrieval Contracts
    "RagRetrievalRequest",
    "RagRetrievalCandidate",
    "RagRetrievalResult",
    # Vector Point
    "CanonicalVectorPoint",
    # Query Plan
    "RagQueryPlan",
    # Citation
    "Citation",
    # Generation
    "IndexGeneration",
    "GenerationConfig",
    "GenerationState",
    # Embedding Cache
    "EmbeddingCacheKey",
    "EmbeddingCacheEntry",
    # Outbox
    "OutboxEvent",
    "OutboxState",
    "OutboxOperation",
    # Provenance
    "ProvenanceRecord",
    # Policy
    "RagPolicy",
    "DEFAULT_POLICY",
    # Model Registry
    "RagModelRegistration",
]

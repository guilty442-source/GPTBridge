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
no-new-evidence stop, Qdrant payload-filter construction, index-profile
selection, and the two cache kinds (embedding vs retrieval).

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist; deny-by-default.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence

from psycopg import Connection

from shared_layer.database.query_allowlist import get_query

_logger = logging.getLogger("gptbridge.ragcapacity")

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
class QdrantIndexProfile:
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
    qdrant_profiles: tuple[QdrantIndexProfile, ...] = field(default_factory=lambda: (
        QdrantIndexProfile("small", m=16, ef_construct=100, max_points=100_000),
        QdrantIndexProfile("medium", m=32, ef_construct=200, max_points=1_000_000),
        QdrantIndexProfile("large", m=64, ef_construct=400, max_points=10_000_000),
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
            "qdrant_profiles": {
                p.name: _asdict(p) for p in self.qdrant_profiles
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
            QdrantIndexProfile(name=k, **{k2: v for k2, v in p.items() if k2 != "name"})
            for k, p in (doc.get("qdrant_profiles") or {}).items()
        ) or tuple([
            p for p in RagCapacityPolicy.__dataclass_fields__["qdrant_profiles"].default
        ])
        return cls(
            policy_version=str(doc.get("policy_version") or "rag-capacity-v1"),
            ingestion=ingest,
            embedding=embed,
            retrieval=retr,
            reranker=rerank,
            generation=gen,
            qdrant_profiles=profiles,
            cache=CachePolicy(**{**_asdict(CachePolicy()), **(doc.get("cache") or {})}),
            tier_defaults=TierDefaults(
                **{**{k: v for k, v in _asdict(TierDefaults()).items()}, **(doc.get("tier_defaults") or {})}
            ),
        )


def _asdict(obj: Any) -> dict[str, Any]:
    from dataclasses import asdict
    return asdict(obj)


DEFAULT_CAPACITY_POLICY = RagCapacityPolicy()


# ============================================================================
# Database-backed policy / queue / trace / latency / generation wrappers
# ============================================================================

def current_capacity_policy(connection: Connection[Any]) -> Optional[RagCapacityPolicy]:
    """Load the active capacity policy from PostgreSQL."""
    with connection.cursor() as cur:
        cur.execute(get_query("ragpolicy.current"))
        row = cur.fetchone()
    if row is None or row[0] is None:
        _logger.warning("no active RAG capacity policy in PostgreSQL; using defaults")
        return None
    doc = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return RagCapacityPolicy.from_json({**doc, **{"policy_version": doc.get("policy_version")}})


def upsert_capacity_policy(
    connection: Connection[Any],
    policy: RagCapacityPolicy,
) -> str:
    """Persist a capacity policy and make it the active one."""
    j = policy.to_json()
    with connection.cursor() as cur:
        cur.execute(
            get_query("ragpolicy.upsert"),
            (
                policy.policy_version,
                json.dumps(j["ingestion"]),
                json.dumps(j["embedding"]),
                json.dumps(j["retrieval"]),
                json.dumps(j["reranker"]),
                json.dumps(j["generation"]),
                json.dumps(j["qdrant_profiles"]),
                json.dumps(j["cache"]),
                json.dumps(j["tier_defaults"]),
            ),
        )
        row = cur.fetchone()
        return str(row[0])


def set_queue_state(
    connection: Connection[Any],
    queue_name: str,
    state: QueueState,
    *,
    threshold_depth: int = 1000,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    if queue_name not in QUEUE_NAMES:
        raise ValueError(f"unknown queue: {queue_name}")
    with connection.cursor() as cur:
        cur.execute(
            get_query("ragpolicy.queue.set"),
            (queue_name, state.value, int(threshold_depth), reason),
        )
        row = cur.fetchone()
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def queue_admission(
    connection: Connection[Any],
    queue_name: str,
    depth: int,
    *,
    workload: str = "ingestion",
) -> dict[str, Any]:
    """Ask PostgreSQL what to do with new work for a bounded queue.

    Returns a decision dict; ``decision`` is one of QueueDecision values.
    ``INDEX_PENDING`` means: accept the metadata now, defer embedding/indexing
    (never a timeout just because the embedding queue is deep).
    """
    if queue_name not in QUEUE_NAMES:
        raise ValueError(f"unknown queue: {queue_name}")
    with connection.cursor() as cur:
        cur.execute(
            get_query("ragpolicy.queue.admit"),
            (queue_name, int(depth), workload),
        )
        row = cur.fetchone()
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def record_phase_latency(
    connection: Connection[Any],
    *,
    request_id: str,
    phase: str,
    elapsed_ms: float,
) -> int:
    """Record one query phase timing (scope/embed/qdrant/fts/fusion/reranker/
    context/generation).  Throttle callers: one row per phase per request."""
    with connection.cursor() as cur:
        cur.execute(
            get_query("ragpolicy.phase_latency.record"),
            (request_id, phase, float(elapsed_ms)),
        )
        row = cur.fetchone()
        return int(row[0])


def record_retrieval_trace(
    connection: Connection[Any],
    *,
    request_id: str,
    rag_type: str,
    round_no: int,
    query: str,
    rewritten_query: Optional[str] = None,
    hit_ids: Sequence[str] = (),
    evidence_score: Optional[float] = None,
    stop_reason: Optional[str] = None,
    module_ids: Sequence[str] = (),
    generation_id: Optional[str] = None,
    policy_version: Optional[str] = None,
) -> int:
    """Record one round of an agentic retrieval trace."""
    with connection.cursor() as cur:
        cur.execute(
            get_query("ragpolicy.trace.record"),
            (
                request_id, rag_type, int(round_no), query, rewritten_query,
                json.dumps(list(hit_ids)), evidence_score, stop_reason,
                json.dumps(list(module_ids)), generation_id, policy_version,
            ),
        )
        row = cur.fetchone()
        return int(row[0])


def initialize_generation(
    connection: Connection[Any],
    *,
    logical_alias: str,
    schema_version: str,
    embedding_model: str,
    embedding_dimension: int,
) -> dict[str, Any]:
    """Register the next physical generation (g000N) under a logical alias."""
    with connection.cursor() as cur:
        cur.execute(
            get_query("ragpolicy.generation.init"),
            (logical_alias, schema_version, embedding_model, int(embedding_dimension)),
        )
        row = cur.fetchone()
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def advance_generation(
    connection: Connection[Any],
    generation_id: str,
    target: str,
    *,
    verification: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Advance a generation: BUILDING -> VERIFYING -> ACTIVE -> RETIRED/FAILED.

    ACTIVE records the alias-swap intent (the Qdrant client performs the swap
    owning the physical collection; this registry is the durable record of it).
    """
    with connection.cursor() as cur:
        cur.execute(
            get_query("ragpolicy.generation.advance"),
            (generation_id, target, json.dumps(verification or {})),
        )
        row = cur.fetchone()
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def list_rag_slo(connection: Connection[Any]) -> list[dict[str, Any]]:
    """List active RAG SLO targets."""
    with connection.cursor() as cur:
        cur.execute(get_query("ragpolicy.slo.list"))
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def list_queue_states(connection: Connection[Any]) -> list[dict[str, Any]]:
    """Return all persisted queue admission states."""
    with connection.cursor() as cur:
        cur.execute(get_query("ragpolicy.queue.list"))
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def list_generations(
    connection: Connection[Any],
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent Qdrant generation registry entries."""
    with connection.cursor() as cur:
        cur.execute(get_query("ragpolicy.generation.list"), (int(limit),))
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def list_retrieval_traces(
    connection: Connection[Any],
    request_id: str,
) -> list[dict[str, Any]]:
    """Return retrieval trace rows for a specific request."""
    with connection.cursor() as cur:
        cur.execute(get_query("ragpolicy.trace.list"), (request_id,))
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def list_phase_latency(
    connection: Connection[Any],
    request_id: str,
) -> list[dict[str, Any]]:
    """Return per-phase latency measurements for a specific request."""
    with connection.cursor() as cur:
        cur.execute(get_query("ragpolicy.latency.list"), (request_id,))
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


# ============================================================================
# Pure admission helper (usable without PostgreSQL, mirrors the SQL function)
# ============================================================================

def admit(
    state: QueueState,
    depth: int,
    *,
    threshold: int = 1000,
    workload: str = "ingestion",
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """In-process mirror of gptbridge_ragpolicy.queue_admission()."""
    base = {"queue_state": state.value, "depth": int(depth), "threshold": threshold}
    if state is QueueState.PAUSED:
        return {**base, "decision": QueueDecision.REJECT.value,
                "reason": reason or "queue paused"}
    if state is QueueState.DRAINING:
        return {**base, "decision": QueueDecision.REJECT.value,
                "reason": "queue draining; no new work"}
    if state is QueueState.ACCEPTING:
        return {**base, "decision": QueueDecision.ACCEPT.value, "reason": None}
    # THROTTLED
    if depth < threshold:
        return {**base, "decision": QueueDecision.ACCEPT.value,
                "reason": "below threshold"}
    if workload == "ingestion":
        return {**base, "decision": QueueDecision.INDEX_PENDING.value,
                "reason": "embedding queue over threshold; accept metadata, defer indexing"}
    return {**base, "decision": QueueDecision.DEFER.value,
            "reason": "queue over threshold", "retry_after_seconds": 2.0}


# ============================================================================
# Query-engine helpers (pure Python — adopt directly in the pipeline)
# ============================================================================

def build_qdrant_filter(
    *,
    module_ids: Sequence[str],
    generation_id: Optional[str] = None,
    classification_max: Optional[str] = None,
    tombstoned: bool = False,
    rag_types: Optional[Sequence[str]] = None,
    tier: Optional[str] = None,
    extra: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build a Qdrant Filter for payload pre-filtering (down-push, NOT
    post-filter).  Every must-clause is pushed to Qdrant before ANN.

    classification_max uses the ordering PUBLIC < INTERNAL < PRIVATE < RESTRICTED.
    """
    ordering = {"public": 0, "internal": 1, "private": 2, "restricted": 3}
    must: list[dict[str, Any]] = []
    if module_ids:
        must.append({"key": "module_id", "match": {"any": list(module_ids)}})
    if generation_id:
        must.append({"key": "generation_id", "match": {"value": generation_id}})
    if classification_max is not None:
        allowed = [k for k, v in ordering.items() if v <= ordering.get(classification_max.lower(), 3)]
        must.append({"key": "data_category", "match": {"any": allowed}})
    must.append({"key": "tombstoned", "match": {"value": bool(tombstoned)}})
    if rag_types:
        must.append({"key": "rag_types", "match": {"any": list(rag_types)}})
    if tier:
        must.append({"key": "tier", "match": {"value": tier}})
    if extra:
        must.extend(extra)
    return {"must": must}


def select_index_profile(
    profiles: Sequence[QdrantIndexProfile],
    points_count: int,
) -> QdrantIndexProfile:
    """Pick the HNSW profile whose max_points first covers the collection size.

    Chosen by data volume now; revisit via benchmark (recall/latency/memory)
    before believing the preset is still right.
    """
    if not profiles:
        return QdrantIndexProfile("small")
    candidates = [p for p in profiles if p.max_points >= points_count]
    if candidates:
        return min(candidates, key=lambda p: p.max_points)
    return max(profiles, key=lambda p: p.max_points)


def reranker_batches(
    candidates: Sequence[Any],
    *,
    batch_size: int = 16,
    max_candidates: int = 40,
) -> list[list[Any]]:
    """Batch reranker candidates (limit total first, then chunk by batch).

    80 candidates with a 40 limit, batch 16 -> 3 batches (16/16/8), never 5
    and never 80 individual model calls.
    """
    capped = list(candidates[: max(1, int(max_candidates))])
    size = max(1, int(batch_size))
    return [capped[i:i + size] for i in range(0, len(capped), size)]


def enforce_diversity(
    candidates: Sequence[Any],
    *,
    max_chunks_per_resource: int = 3,
    resource_key: str = "resource_id",
) -> list[Any]:
    """Limit how many chunks one resource may contribute (context diversity).

    Prevents a single high-scoring doc from crowding out Doc B / Doc C.
    """
    limit = max(1, int(max_chunks_per_resource))
    seen: dict[Any, int] = {}
    result: list[Any] = []
    for cand in candidates:
        rid = cand.get(resource_key) if isinstance(cand, dict) else getattr(cand, resource_key, None)
        if rid is None:
            result.append(cand)
            continue
        n = seen.get(rid, 0)
        if n < limit:
            seen[rid] = n + 1
            result.append(cand)
    return result


def merge_adjacent_chunks(
    chunks: Sequence[Any],
    *,
    adjacency_gap: int = 1,
    sequence_key: str = "sequence",
    id_key: str = "chunk_id",
) -> list[dict[str, Any]]:
    """Merge contiguous chunk runs before generating (R1 = 17-19).

    Reduces duplicate overlap, citation count and token waste for the LLM
    while preserving the original chunk ids for traceability.  Items may be
    dicts with ``chunk_id``/``sequence`` or objects exposing those attrs.
    """
    def _val(item: Any, key: str) -> Any:
        return item.get(key) if isinstance(item, dict) else getattr(item, key, None)

    ordered = sorted(chunks, key=lambda c: int(_val(c, sequence_key) or 0))
    merged: list[dict[str, Any]] = []
    for chunk in ordered:
        seq = int(_val(chunk, sequence_key) or 0)
        cid = _val(chunk, id_key)
        if merged and merged[-1]["end_sequence"] >= seq - adjacency_gap:
            prev = merged[-1]
            prev["end_sequence"] = max(prev["end_sequence"], seq)
            prev["chunk_ids"].append(cid)
            prev["sequence_start"] = prev["sequence_start"]
        else:
            merged.append({
                "chunk_ids": [cid],
                "sequence_start": seq,
                "end_sequence": seq,
                "merged": False,
            })
    for seg in merged:
        seg["merged"] = len(seg["chunk_ids"]) > 1
        seg["chunk_ids"] = list(dict.fromkeys(seg["chunk_ids"]))
    return merged


def has_new_evidence(previous_ids: Sequence[str], current_ids: Sequence[str]) -> bool:
    """True when the current round added at least one new hit id."""
    prev = set(previous_ids or ())
    return any(cid not in prev for cid in current_ids)


def agentic_stop_reason(
    *,
    round_no: int,
    max_rounds: int,
    time_up: bool,
    evidence_sufficient: bool,
    new_evidence: bool,
) -> Optional[str]:
    """Decide whether the agentic loop must stop and why.

    Priority: time budget > evidence sufficient > no new evidence > max rounds.
    """
    if time_up:
        return AgenticStop.TIME_BUDGET.value
    if evidence_sufficient:
        return AgenticStop.EVIDENCE_SUFFICIENT.value
    if round_no >= max_rounds:
        return AgenticStop.MAX_ROUNDS.value
    if not new_evidence:
        return AgenticStop.NO_NEW_EVIDENCE.value
    return None


def retrieval_cache_key(
    *,
    query: str,
    module_scope: Sequence[str],
    generation_id: str,
    policy_version: str,
) -> str:
    """Stable retrieval-cache key.  Generation change invalidates it automatically."""
    normalized = " ".join(query.lower().split())
    return hashlib.sha256(
        f"{normalized}\x00{','.join(sorted(module_scope))}\x00"
        f"{generation_id}\x00{policy_version}".encode("utf-8")
    ).hexdigest()


def embedding_cache_key(*, text_hash: str, model: str, dimension: int) -> str:
    """Stable embedding-cache key: hash + model + dimension.  Never the raw text."""
    return f"{text_hash}:{model}:{dimension}"


# ============================================================================
# Two cache kinds — embedding (stable) vs retrieval (short-lived)
# ============================================================================

class _TTLCache:
    def __init__(self, ttl_seconds: int, max_size: int = 4096) -> None:
        self._ttl = ttl_seconds
        self._max = max_size
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._cache[key]
                self._misses += 1
                return None
            self._hits += 1
            return value

    def _put(self, key: str, value: Any) -> None:
        with self._lock:
            if key not in self._cache and len(self._cache) >= self._max:
                seq = sorted(self._cache.items(), key=lambda kv: kv[1][1])
                self._cache.pop(seq[0][0], None)
            self._cache[key] = (value, time.monotonic() + self._ttl)

    def _drop(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "max_size": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (self._hits / total) if total > 0 else 0,
            }


class EmbeddingCache(_TTLCache):
    """Embedding cache: text_hash + model + dimension.  Very stable, long TTL.

    Stores vector bytes only — never content or paths.
    """

    def __init__(self, ttl_seconds: int = 86_400, max_size: int = 16384) -> None:
        super().__init__(ttl_seconds, max_size)

    def get(self, text_hash: str, model: str, dimension: int) -> Optional[tuple[float, ...]]:
        return self._get(embedding_cache_key(text_hash=text_hash, model=model, dimension=dimension))

    def put(self, text_hash: str, model: str, dimension: int, vector: Sequence[float]) -> None:
        self._put(
            embedding_cache_key(text_hash=text_hash, model=model, dimension=dimension),
            tuple(vector),
        )


class RetrievalCache(_TTLCache):
    """Retrieval/reranker result cache: query + scope + generation + policy.

    Short-lived; any generation change splits the key space so stale hits can
    never survive an index swap.  Cache retrieval candidates and reranker
    scores — NOT the final LLM answer.
    """

    def __init__(self, ttl_seconds: int = 60, max_size: int = 8192) -> None:
        super().__init__(ttl_seconds, max_size)

    def get(
        self,
        *,
        query: str,
        module_scope: Sequence[str],
        generation_id: str,
        policy_version: str,
    ) -> Optional[Any]:
        key = retrieval_cache_key(
            query=query,
            module_scope=module_scope,
            generation_id=generation_id,
            policy_version=policy_version,
        )
        return self._get(key)

    def put(
        self,
        *,
        query: str,
        module_scope: Sequence[str],
        generation_id: str,
        policy_version: str,
        value: Any,
    ) -> None:
        key = retrieval_cache_key(
            query=query,
            module_scope=module_scope,
            generation_id=generation_id,
            policy_version=policy_version,
        )
        self._put(key, value)


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
    "QdrantIndexProfile",
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
    "build_qdrant_filter",
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
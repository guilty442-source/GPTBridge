"""RAG capacity in-process helpers: admission, lifecycle gate, query-engine utils.

Pure Python mirrors of the PostgreSQL admission logic plus pipeline helpers —
usable without a database connection.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from shared_layer.database.vector_capacity import CapacityDecision

from .rag_capacity_models import (
    AgenticStop,
    QueueDecision,
    QueueState,
    VectorIndexProfile,
)


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
# Lifecycle gate — typed admission for vector build / cleanup operations
# ============================================================================

LIFECYCLE_OPERATIONS: frozenset[str] = frozenset({"build", "cleanup"})
LIFECYCLE_QUEUE = "rebuild"


@dataclass(frozen=True)
class LifecycleGateDecision:
    """Typed admission verdict for a generation build / cleanup operation.

    Combines the bounded rebuild-queue admission with the vector capacity
    verdict.  A missing capacity decision is not silently compliant: it is
    surfaced as ``capacity_verdict=None`` so callers can distinguish
    "checked and allowed" from "not configured".
    """
    operation: str
    allowed: bool
    reason: str
    queue: Optional[dict[str, Any]] = None
    capacity_verdict: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "allowed": self.allowed,
            "reason": self.reason,
            "queue": self.queue,
            "capacity_verdict": self.capacity_verdict,
        }


def gate_lifecycle_operation(
    operation: str,
    *,
    queue_state: Optional[QueueState] = None,
    queue_depth: int = 0,
    queue_threshold: int = 1000,
    capacity: Optional[CapacityDecision] = None,
) -> LifecycleGateDecision:
    """Admit or refuse a vector lifecycle operation (build / cleanup).

    Refuses when the rebuild queue is not ACCEPTING or when the injected
    capacity decision is not ALLOW.  With no capacity decision the gate
    reports ``capacity_verdict=None`` — the caller stays responsible for
    not treating that as a verified success.
    """
    if operation not in LIFECYCLE_OPERATIONS:
        raise ValueError(f"unknown lifecycle operation: {operation}")

    queue_decision: Optional[dict[str, Any]] = None
    if queue_state is not None:
        queue_decision = admit(
            queue_state,
            int(queue_depth),
            threshold=int(queue_threshold),
            workload=LIFECYCLE_QUEUE,
        )
        if queue_decision.get("decision") != QueueDecision.ACCEPT.value:
            return LifecycleGateDecision(
                operation=operation,
                allowed=False,
                reason=f"queue-not-accepting: {queue_decision.get('decision')}",
                queue=queue_decision,
                capacity_verdict=(
                    capacity.verdict.value if capacity is not None else None
                ),
            )

    if capacity is not None:
        return LifecycleGateDecision(
            operation=operation,
            allowed=capacity.allowed,
            reason=capacity.reason,
            queue=queue_decision,
            capacity_verdict=capacity.verdict.value,
        )

    return LifecycleGateDecision(
        operation=operation,
        allowed=True,
        reason="allowed (capacity not configured)",
        queue=queue_decision,
        capacity_verdict=None,
    )


# ============================================================================
# Query-engine helpers (pure Python — adopt directly in the pipeline)
# ============================================================================

def build_vector_filter(
    *,
    module_ids: Sequence[str],
    generation_id: Optional[str] = None,
    classification_max: Optional[str] = None,
    tombstoned: bool = False,
    rag_types: Optional[Sequence[str]] = None,
    tier: Optional[str] = None,
    extra: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build a vector Filter for payload pre-filtering (down-push, NOT
    post-filter).  Every must-clause is pushed to the vector engine before ANN.

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
    profiles: Sequence[VectorIndexProfile],
    points_count: int,
) -> VectorIndexProfile:
    """Pick the HNSW profile whose max_points first covers the collection size.

    Chosen by data volume now; revisit via benchmark (recall/latency/memory)
    before believing the preset is still right.
    """
    if not profiles:
        return VectorIndexProfile("small")
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

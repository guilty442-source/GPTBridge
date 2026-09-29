"""RAG capacity store: PostgreSQL-backed policy/queue/trace/generation wrappers (migration 088)."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Sequence

from psycopg import Connection

from shared_layer.database.query_allowlist import get_query

from .rag_capacity_models import (
    QUEUE_NAMES,
    QueueState,
    RagCapacityPolicy,
)

_logger = logging.getLogger("gptbridge.ragcapacity")


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
                json.dumps(j["vector_profiles"]),
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
    """Record one query phase timing (scope/embed/vector/fts/fusion/reranker/
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

    ACTIVE records the alias-swap intent (the vector engine performs the swap
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
    """Return recent vector generation registry entries."""
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

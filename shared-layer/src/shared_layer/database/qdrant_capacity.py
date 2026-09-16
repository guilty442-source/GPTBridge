"""Qdrant Capacity Planner (A369 + Qdrant rebuildable projection).

Estimates collection bytes:

    vector_count
        x dimension x 4 bytes         (float32 vector storage)
        x storage_overhead            (segment/page overhead)
        + vector_count x payload_bytes (per-point payload)
        x index_overhead              (HNSW graph + quantization)

Embedding model migrations need room for two collections at once —
``dual_collection_headroom`` reports the extra bytes required while
the old-generation collection stays queryable (A369: prior-generation
vectors stay stale-but-available until the successor is verified).

Usage:
    from shared_layer.database.qdrant_capacity import (
        QdrantCollectionSpec, estimate_collection_bytes,
        dual_collection_headroom,
    )
"""
from __future__ import annotations

from dataclasses import dataclass

FLOAT32_BYTES = 4


@dataclass(frozen=True)
class QdrantCollectionSpec:
    vector_count: int
    dimension: int
    payload_bytes_per_point: int = 512
    storage_overhead: float = 1.10  # segment/page overhead factor
    index_overhead: float = 1.20    # HNSW graph + quantization factor

    def __post_init__(self) -> None:
        if self.vector_count < 0 or self.dimension <= 0:
            raise ValueError("vector_count >= 0 and dimension > 0 required")
        if self.storage_overhead < 1.0 or self.index_overhead < 1.0:
            raise ValueError("overhead factors must be >= 1.0")


@dataclass(frozen=True)
class QdrantEstimate:
    vector_bytes: int
    payload_bytes: int
    index_bytes: int
    total_bytes: int


def estimate_collection_bytes(spec: QdrantCollectionSpec) -> QdrantEstimate:
    """Bytes one collection needs under the declared spec."""
    raw_vectors = spec.vector_count * spec.dimension * FLOAT32_BYTES
    vector_bytes = int(raw_vectors * spec.storage_overhead)
    payload_bytes = spec.vector_count * spec.payload_bytes_per_point
    index_bytes = int((vector_bytes + payload_bytes)
                      * (spec.index_overhead - 1.0))
    return QdrantEstimate(
        vector_bytes=vector_bytes,
        payload_bytes=payload_bytes,
        index_bytes=index_bytes,
        total_bytes=vector_bytes + payload_bytes + index_bytes,
    )


def dual_collection_headroom(
    current: QdrantCollectionSpec, successor: QdrantCollectionSpec
) -> int:
    """Extra bytes needed while old and new collections coexist.

    The successor is typically larger (new embedding dimension or
    full re-index).  The answer is the successor's total, because the
    current collection is already provisioned.
    """
    return estimate_collection_bytes(successor).total_bytes


@dataclass(frozen=True)
class QdrantCapacityReport:
    current: QdrantEstimate
    successor: QdrantEstimate | None
    headroom_required_bytes: int
    fits_in: bool
    available_bytes: int


def plan_capacity(
    current: QdrantCollectionSpec,
    *,
    available_bytes: int,
    successor: QdrantCollectionSpec | None = None,
) -> QdrantCapacityReport:
    """Decide whether current (+ optional successor) fits the budget."""
    current_est = estimate_collection_bytes(current)
    successor_est = (
        estimate_collection_bytes(successor) if successor else None
    )
    required = current_est.total_bytes + (
        successor_est.total_bytes if successor_est else 0
    )
    return QdrantCapacityReport(
        current=current_est,
        successor=successor_est,
        headroom_required_bytes=required,
        fits_in=required <= available_bytes,
        available_bytes=available_bytes,
    )


__all__ = [
    "FLOAT32_BYTES",
    "QdrantCapacityReport",
    "QdrantCollectionSpec",
    "QdrantEstimate",
    "dual_collection_headroom",
    "estimate_collection_bytes",
    "plan_capacity",
]

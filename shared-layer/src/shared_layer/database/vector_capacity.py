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
from enum import Enum
from typing import Any, Optional

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


class CapacityVerdict(str, Enum):
    """Typed outcome of a capacity gate for one Qdrant operation."""
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CapacityDecision:
    """Typed, fail-closed capacity verdict for a lifecycle operation.

    ``UNKNOWN`` means the budget could not be established; it is never a
    silent ALLOW — the operation must be refused until capacity is known.
    """
    operation: str
    verdict: CapacityVerdict
    allowed: bool
    reason: str
    required_bytes: Optional[int] = None
    available_bytes: Optional[int] = None
    headroom_bytes: Optional[int] = None
    report: Optional[QdrantCapacityReport] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "verdict": self.verdict.value,
            "allowed": self.allowed,
            "reason": self.reason,
            "required_bytes": self.required_bytes,
            "available_bytes": self.available_bytes,
            "headroom_bytes": self.headroom_bytes,
        }


def authorize_operation(
    operation: str,
    *,
    current: QdrantCollectionSpec | None = None,
    successor: QdrantCollectionSpec | None = None,
    available_bytes: int | None,
    safety_margin: float = 0.0,
) -> CapacityDecision:
    """Capacity gate for a Qdrant lifecycle operation (build / cleanup).

    Fail-closed rules:
      * ``available_bytes is None`` -> UNKNOWN, refused.  An unknown budget
        must never be treated as unlimited.
      * ``required_bytes > budget`` -> REJECT ("capacity-limit-reached").
      * Otherwise ALLOW with the full report attached.

    ``headroom_bytes`` is ``dual_collection_headroom`` when both a current
    and a successor collection coexist (A369 dual-collection migration).
    """
    if not operation:
        raise ValueError("operation must be a non-empty string")
    if available_bytes is None:
        return CapacityDecision(
            operation=operation,
            verdict=CapacityVerdict.UNKNOWN,
            allowed=False,
            reason="capacity-budget-unavailable",
        )
    if available_bytes < 0:
        raise ValueError("available_bytes must be >= 0")
    if not 0.0 <= safety_margin < 1.0:
        raise ValueError("safety_margin must be in [0, 1)")
    if current is None and successor is None:
        return CapacityDecision(
            operation=operation,
            verdict=CapacityVerdict.REJECT,
            allowed=False,
            reason="no-collection-spec",
            available_bytes=available_bytes,
        )

    current_est = estimate_collection_bytes(current) if current is not None else None
    successor_est = (
        estimate_collection_bytes(successor) if successor is not None else None
    )
    required = (current_est.total_bytes if current_est else 0) + (
        successor_est.total_bytes if successor_est else 0
    )
    budget = int(available_bytes * (1.0 - safety_margin))
    headroom = (
        dual_collection_headroom(current, successor)
        if current is not None and successor is not None
        else None
    )
    report = QdrantCapacityReport(
        current=current_est or QdrantEstimate(0, 0, 0, 0),
        successor=successor_est,
        headroom_required_bytes=required,
        fits_in=required <= budget,
        available_bytes=budget,
    )
    if report.fits_in:
        return CapacityDecision(
            operation=operation,
            verdict=CapacityVerdict.ALLOW,
            allowed=True,
            reason="within-budget",
            required_bytes=required,
            available_bytes=available_bytes,
            headroom_bytes=headroom,
            report=report,
        )
    return CapacityDecision(
        operation=operation,
        verdict=CapacityVerdict.REJECT,
        allowed=False,
        reason="capacity-limit-reached",
        required_bytes=required,
        available_bytes=available_bytes,
        headroom_bytes=headroom,
        report=report,
    )


__all__ = [
    "FLOAT32_BYTES",
    "CapacityDecision",
    "CapacityVerdict",
    "QdrantCapacityReport",
    "QdrantCollectionSpec",
    "QdrantEstimate",
    "authorize_operation",
    "dual_collection_headroom",
    "estimate_collection_bytes",
    "plan_capacity",
]

"""Step plans and the fixed compensation table.

Compensation is decided per step *up front* (never improvised at failure
time) and the strategies are deliberately not "undo everything": audit
appends stay, published data is invalidated or superseded rather than
deleted, and cross-engine residue is reconciled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .types import Engine, OutcomeStrategy


@dataclass(frozen=True)
class StepSpec:
    step_id: str
    step_order: int
    engine: Engine
    action_type: str
    strategy: OutcomeStrategy
    idempotent: bool = True
    long_running: bool = False
    description: str = ""


@dataclass(frozen=True)
class StepPlan:
    operation_type: str
    steps: tuple[StepSpec, ...]

    def ordered(self) -> tuple[StepSpec, ...]:
        return tuple(sorted(self.steps, key=lambda step: step.step_order))

    def step(self, step_id: str) -> StepSpec:
        for candidate in self.steps:
            if candidate.step_id == step_id:
                return candidate
        raise KeyError(f"UNKNOWN_STEP:{step_id}")


# Point 23 of the contract: per-step failure strategy, fixed at design time.
COMPENSATION_TABLE: dict[str, OutcomeStrategy] = {
    "PG_RESOURCE_RESERVE": OutcomeStrategy.SUPERSEDE,
    "FILE_WRITE": OutcomeStrategy.COMPENSATE,
    "PG_CHUNK_METADATA": OutcomeStrategy.INVALIDATE,
    "MODEL_EMBED": OutcomeStrategy.RETRY_IDEMPOTENT,
    "QDRANT_UPSERT": OutcomeStrategy.INVALIDATE,
    "AUDIT_APPEND": OutcomeStrategy.APPEND_ONLY,
    "TRANSPORT_REQUEST": OutcomeStrategy.RETRY_IDEMPOTENT,
    "SQLITE_FALLBACK": OutcomeStrategy.RECONCILE,
    "PG_MARK_READY": OutcomeStrategy.VERIFY_THEN_DECIDE,
    "QDRANT_DELETE": OutcomeStrategy.RECONCILE,
    "FILE_DELETE": OutcomeStrategy.RECONCILE,
}

# Formal RAG ingest plan: publish only after verification (publish barrier).
RAG_INGEST_PLAN = StepPlan(
    operation_type="rag_ingest",
    steps=(
        StepSpec("REGISTER_RESOURCE", 1, Engine.POSTGRESQL, "PG_RESOURCE_RESERVE",
                 COMPENSATION_TABLE["PG_RESOURCE_RESERVE"], description="reserve resource id, status=PREPARING"),
        StepSpec("WRITE_SOURCE_FILE", 2, Engine.FILESYSTEM, "FILE_WRITE",
                 COMPENSATION_TABLE["FILE_WRITE"], description="temp -> fsync -> hash -> atomic rename"),
        StepSpec("VERIFY_FILE_HASH", 3, Engine.FILESYSTEM, "FILE_VERIFY",
                 OutcomeStrategy.VERIFY_THEN_DECIDE, description="hash must match the reservation"),
        StepSpec("CREATE_CHUNKS", 4, Engine.POSTGRESQL, "PG_CHUNK_METADATA",
                 COMPENSATION_TABLE["PG_CHUNK_METADATA"], description="chunk rows PREPARING"),
        StepSpec("EMBED_CHUNKS", 5, Engine.MODEL, "MODEL_EMBED",
                 COMPENSATION_TABLE["MODEL_EMBED"], long_running=True),
        StepSpec("UPSERT_POINTS", 6, Engine.QDRANT, "QDRANT_UPSERT",
                 COMPENSATION_TABLE["QDRANT_UPSERT"], description="stable point ids, idempotent upsert"),
        StepSpec("VERIFY_POINTS", 7, Engine.QDRANT, "QDRANT_VERIFY",
                 OutcomeStrategy.VERIFY_THEN_DECIDE),
        StepSpec("MARK_INDEX_READY", 8, Engine.POSTGRESQL, "PG_MARK_READY",
                 COMPENSATION_TABLE["PG_MARK_READY"], description="flip publish barrier to READY"),
    ),
)


@dataclass
class StepResult:
    step_id: str
    status: str
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    result_hash: str = ""
    error_code: str = ""


__all__ = [
    "COMPENSATION_TABLE",
    "RAG_INGEST_PLAN",
    "StepPlan",
    "StepResult",
    "StepSpec",
]

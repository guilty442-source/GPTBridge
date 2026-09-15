"""A374 recovery + health surface for CanonicalRagPipeline.

DEGRADED → RECONCILING → CANONICAL is only allowed after honest parity
(index_state chunk coverage vs Qdrant points, point-id / content-hash /
embedding-version completeness) plus queue-drain verification.  A failed
attempt stays bounded at DEGRADED and is surfaced as the derived
RECONCILIATION_FAILED state.
"""

from __future__ import annotations

import logging
from typing import Any

from .runtime_state import (
    CanonicalCheckError,
    RagRuntimeState,
    TransitionError,
)

_logger = logging.getLogger("gptbridge.rag")


class PipelineRecoveryMixin:
    """Recovery driver + health reporting for the canonical pipeline."""

    async def attempt_recovery(self) -> RagRuntimeState:
        """A374 recovery: DEGRADED → RECONCILING → CANONICAL only after
        parity + queue-drain verification; failure stays bounded at
        DEGRADED and is surfaced as RECONCILIATION_FAILED.  No-op outside
        DEGRADED or while canonical services are down."""
        if self.state != RagRuntimeState.DEGRADED:
            return self.state
        if not (self.qdrant.is_healthy() and self.postgresql.is_healthy()):
            return self.state
        try:
            self._state_machine.begin_reconciliation(
                qdrant_healthy=True, postgresql_healthy=True
            )
        except (CanonicalCheckError, TransitionError) as exc:
            self._state_machine.report_canonical_failure(
                f"reconciliation could not start: {exc}"
            )
            return self.state
        try:
            parity = await self._recovery_parity()
        except Exception as exc:
            return self._state_machine.fail_reconciliation(
                f"parity check failed: {exc}"
            )
        return self._state_machine.complete_reconciliation(**parity)

    async def _recovery_parity(self) -> dict[str, bool]:
        """Honest parity: index_state chunk coverage vs Qdrant points,
        plus point-id / content-hash / embedding-version completeness."""
        summary = await self.postgresql.index_state_summary(
            self.config.embedding_model, self.config.embedding_dimension
        )
        points = self.qdrant.points_count()
        if summary is None or points is None:
            raise CanonicalCheckError("parity inputs unavailable")
        return {
            "counts_match": summary["total_chunks"] == points,
            "ids_match": summary["missing_point_ids"] == 0,
            "hashes_match": summary["missing_hashes"] == 0,
            "versions_match": summary["mismatched_versions"] == 0,
        }

    async def health_check(self) -> dict[str, Any]:
        """Health check for all components (A374)."""
        await self.attempt_recovery()
        state = self._state_machine.state
        is_canonical = state == RagRuntimeState.CANONICAL
        result = {
            "pipeline_ready": self.is_ready(),
            "state": state.value,
            "effective_state": self._state_machine.effective_state,
            "reconciliation_failed": self._state_machine.reconciliation_failed,
            "canonical": is_canonical,
            "reconciliation_required": self._state_machine.reconciliation_required,
            "queue_pending": self._queue.pending_count(),
            "queue_complete": self._queue.is_complete(),
            "qdrant": {
                "healthy": self.qdrant.is_healthy(),
                "collection": self.config.collection_name,
            },
            "postgresql": {
                "healthy": self.postgresql.is_healthy(),
            },
            "domain_model": "ok",
            "degraded_pipeline_active": self._degraded_pipeline is not None,
        }
        if self._degraded_pipeline is not None:
            degraded_health = await self._degraded_pipeline.health_check()
            result["degraded_pipeline"] = degraded_health
        return result

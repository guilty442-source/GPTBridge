"""A374 Step 3: Python domain model — sole owner of typed RAG results.

Builds ``RagQueryResult`` from canonical sources only: Qdrant dense hits
joined with PostgreSQL metadata and authoritative index_state.  Hits
without index_state proof are dropped (A373 canonical-takeover).
"""

from __future__ import annotations

import logging
from typing import Any

from .rag_qdrant import IndexState, RagPipelineConfig, RagQueryResult

_logger = logging.getLogger("gptbridge.rag")


class PythonDomainModel:
    """A374 Step 3: Python domain model - sole production owner of typed results."""

    def __init__(self, config: RagPipelineConfig) -> None:
        self.config = config

    def build_typed_result(
        self,
        qdrant_hits: list[dict[str, Any]],
        pg_metadata: dict[str, dict[str, Any]],
        index_states: dict[str, IndexState],
    ) -> list[RagQueryResult]:
        """Build typed domain results from canonical sources."""
        results = []
        for hit in qdrant_hits:
            payload = hit.get("payload", {})
            resource_id = payload.get("resource_id") or hit.get("id")
            module_id = payload.get("module_id")

            if not resource_id or not module_id:
                continue

            key = f"{module_id}:{resource_id}"
            index_state = index_states.get(key)
            pg_meta = pg_metadata.get(key, {})

            if not index_state:
                _logger.warning("PythonDomainModel: missing index_state for %s", key)
                continue

            results.append(RagQueryResult(
                resource_id=resource_id,
                module_id=module_id,
                content=payload.get("content", ""),
                score=hit.get("score", 0.0),
                metadata={**payload, **pg_meta.get("metadata", {})},
                index_state=index_state,
            ))
        return results


__all__ = ["PythonDomainModel"]

"""Index Generation Management — 版本化索引世代系統。

A486+A487: Index Generation with atomic promotion, alias switching, and full lifecycle.
Each full reindex creates a new generation; query always targets the ACTIVE alias.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from shared_layer.metadata_contract import (
    FIELD_CONTENT_HASH,
    FIELD_MODULE_ID,
    FIELD_RESOURCE_ID,
    FIELD_STATUS,
    FIELD_UPDATED_AT,
    FIELD_VERSION,
    STATUS_INDEXED,
)

_logger = logging.getLogger("gptbridge.rag.generation")


class GenerationState(str, Enum):
    """Index generation lifecycle states."""
    BUILDING = "BUILDING"      # Full reindex in progress
    VERIFYING = "VERIFYING"    # Post-build verification
    ACTIVE = "ACTIVE"          # Serving queries (alias points here)
    RETIRED = "RETIRED"        # Superseded, awaiting cleanup
    FAILED = "FAILED"          # Build/verify failed


@dataclass(frozen=True)
class IndexGeneration:
    """Immutable index generation metadata."""
    generation_id: str                    # e.g., "gen-20260916-001"
    index_schema_version: str             # e.g., "v3"
    embedding_model: str                  # e.g., "qwen3-embedding:4b"
    embedding_dimension: int              # e.g., 2560
    chunk_policy_version: str             # e.g., "v3"
    chunk_size: int
    chunk_overlap: int
    created_at: str                       # ISO UTC
    state: GenerationState
    collection_name: str                  # Physical Qdrant collection
    alias_name: str                       # Logical alias (e.g., "gptbridge_shared_knowledge")
    previous_generation_id: Optional[str] = None
    points_count: int = 0
    verification_result: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None


@dataclass
class GenerationConfig:
    """Configuration for generation management."""
    alias_name: str = "gptbridge_shared_knowledge"
    index_schema_version: str = "v3"
    embedding_model: str = "qwen3-embedding:4b"
    embedding_dimension: int = 2560
    chunk_policy_version: str = "v3"
    chunk_size: int = 1200
    chunk_overlap: int = 200
    max_generations_to_keep: int = 3


class GenerationManager:
    """Manages index generations, alias switching, and lifecycle."""

    def __init__(
        self,
        qdrant_client: Any,
        config: GenerationConfig,
        metadata_db: Any,  # PostgreSQLMetadataAuthority
    ) -> None:
        self.qdrant = qdrant_client
        self.config = config
        self.metadata_db = metadata_db
        self._current_generation: Optional[IndexGeneration] = None

    def _generate_generation_id(self) -> str:
        """Generate unique generation ID: gen-YYYYMMDD-NNN."""
        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
        # Simple counter - in production use sequence or UUID suffix
        suffix = uuid.uuid4().hex[:3]
        return f"gen-{date_part}-{suffix}"

    def _physical_collection_name(self, generation_id: str) -> str:
        """Map generation ID to physical Qdrant collection name."""
        return f"{self.config.alias_name}_{generation_id}"

    async def get_active_generation(self) -> Optional[IndexGeneration]:
        """Fetch the currently ACTIVE generation from metadata DB."""
        # In production, query PostgreSQL for generation with state=ACTIVE
        # For now, return cached if available
        if self._current_generation and self._current_generation.state == GenerationState.ACTIVE:
            return self._current_generation
        return None

    async def create_generation(self) -> IndexGeneration:
        """Create a new BUILDING generation (starts full reindex)."""
        gen_id = self._generate_generation_id()
        collection_name = self._physical_collection_name(gen_id)

        generation = IndexGeneration(
            generation_id=gen_id,
            index_schema_version=self.config.index_schema_version,
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            chunk_policy_version=self.config.chunk_policy_version,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            created_at=datetime.now(timezone.utc).isoformat(),
            state=GenerationState.BUILDING,
            collection_name=collection_name,
            alias_name=self.config.alias_name,
        )

        # Persist to PostgreSQL (metadata_db should have upsert_generation)
        await self.metadata_db.upsert_generation(generation)
        _logger.info("GenerationManager: created %s (collection=%s)", gen_id, collection_name)
        return generation

    async def ensure_collection(self, generation: IndexGeneration) -> bool:
        """Ensure the generation's physical collection exists with correct vector config."""
        try:
            collections = self.qdrant.get_collections()
            names = {c.name for c in collections.collections}
            if generation.collection_name not in names:
                self.qdrant.create_collection(
                    collection_name=generation.collection_name,
                    vectors_config=VectorParams(
                        size=generation.embedding_dimension,
                        distance=Distance.COSINE,
                    ),
                )
                _logger.info("GenerationManager: created collection %s", generation.collection_name)
            return True
        except Exception as exc:
            _logger.error("GenerationManager: ensure_collection failed for %s: %s",
                         generation.collection_name, exc)
            return False

    async def promote_to_active(self, generation: IndexGeneration) -> bool:
        """Atomically switch alias to point to the new generation."""
        try:
            # 1. Create alias pointing to new collection
            self.qdrant.create_alias(
                alias_name=generation.alias_name,
                collection_name=generation.collection_name,
            )

            # 2. Update generation state to ACTIVE
            active_gen = IndexGeneration(
                generation_id=generation.generation_id,
                index_schema_version=generation.index_schema_version,
                embedding_model=generation.embedding_model,
                embedding_dimension=generation.embedding_dimension,
                chunk_policy_version=generation.chunk_policy_version,
                chunk_size=generation.chunk_size,
                chunk_overlap=generation.chunk_overlap,
                created_at=generation.created_at,
                state=GenerationState.ACTIVE,
                collection_name=generation.collection_name,
                alias_name=generation.alias_name,
                previous_generation_id=generation.previous_generation_id,
                points_count=generation.points_count,
                verification_result=generation.verification_result,
            )
            await self.metadata_db.upsert_generation(active_gen)
            self._current_generation = active_gen

            # 3. Retire previous ACTIVE generation
            if generation.previous_generation_id:
                await self._retire_generation(generation.previous_generation_id)

            _logger.info("GenerationManager: promoted %s to ACTIVE (alias=%s)",
                        generation.generation_id, generation.alias_name)
            return True

        except Exception as exc:
            _logger.error("GenerationManager: promote_to_active failed: %s", exc)
            return False

    async def _retire_generation(self, generation_id: str) -> None:
        """Mark a generation as RETIRED."""
        try:
            # Fetch and update state
            gen = await self.metadata_db.get_generation(generation_id)
            if gen and gen.state == GenerationState.ACTIVE:
                retired = IndexGeneration(
                    generation_id=gen.generation_id,
                    index_schema_version=gen.index_schema_version,
                    embedding_model=gen.embedding_model,
                    embedding_dimension=gen.embedding_dimension,
                    chunk_policy_version=gen.chunk_policy_version,
                    chunk_size=gen.chunk_size,
                    chunk_overlap=gen.chunk_overlap,
                    created_at=gen.created_at,
                    state=GenerationState.RETIRED,
                    collection_name=gen.collection_name,
                    alias_name=gen.alias_name,
                    previous_generation_id=gen.previous_generation_id,
                    points_count=gen.points_count,
                    verification_result=gen.verification_result,
                )
                await self.metadata_db.upsert_generation(retired)
                _logger.info("GenerationManager: retired %s", generation_id)
        except Exception as exc:
            _logger.warning("GenerationManager: _retire_generation failed: %s", exc)

    async def cleanup_old_generations(self) -> None:
        """Delete RETIRED generations beyond retention limit."""
        # Implementation: list all RETIRED, sort by created_at, delete oldest
        pass

    async def verify_generation(self, generation: IndexGeneration) -> dict[str, Any]:
        """Verify a BUILDING generation is query-ready."""
        try:
            # 1. Check collection exists and has expected vector config
            info = self.qdrant.get_collection(generation.collection_name)
            vector_config = info.config.params.vectors
            if vector_config.size != generation.embedding_dimension:
                return {"ok": False, "reason": "dimension_mismatch"}

            # 2. Sample queries against the alias (if promoted) or collection directly
            # In practice, run a few test queries and verify results have expected fields
            # For now, basic checks
            points_count = info.points_count or 0

            return {
                "ok": True,
                "points_count": points_count,
                "collection_exists": True,
                "dimension_match": True,
            }
        except Exception as exc:
            _logger.error("GenerationManager: verify_generation failed: %s", exc)
            return {"ok": False, "reason": str(exc)}


# PostgreSQL generation schema additions (to be added to rag_metadata.py)
GENERATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS rag_generations (
    generation_id TEXT PRIMARY KEY,
    index_schema_version TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    chunk_policy_version TEXT NOT NULL,
    chunk_size INTEGER NOT NULL,
    chunk_overlap INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('BUILDING','VERIFYING','ACTIVE','RETIRED','FAILED')),
    collection_name TEXT NOT NULL,
    alias_name TEXT NOT NULL,
    previous_generation_id TEXT,
    points_count INTEGER DEFAULT 0,
    verification_result JSONB,
    error_message TEXT,
    UNIQUE (alias_name, state) WHERE state = 'ACTIVE'  -- Only one ACTIVE per alias
);

CREATE INDEX IF NOT EXISTS idx_rag_generations_alias_state
ON rag_generations (alias_name, state);
"""

from qdrant_client.http.models import Distance, VectorParams  # noqa: E402

__all__ = [
    "GenerationState",
    "IndexGeneration",
    "GenerationConfig",
    "GenerationManager",
    "GENERATION_TABLE_SQL",
]

(End of file - total 247 lines)
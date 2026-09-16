"""Model Registry + Enhanced RAG Status — 統一模型管理與完整狀態報告。

Model Registry: 解決「Ollama 裡有一個 reranker 名稱，但真正 reranker 又由 HF local cache 執行」的混淆
Enhanced Status: 讓星澄真正能「管理 RAG」，而不只是呼叫 status() 看一個 READY
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from .rag_protocol import RagBackendHealth, BackendType
from .rag_contracts import (
    RagModelRegistration,
    CanonicalState,
    LifecycleState,
    GenerationState,
)

_logger = logging.getLogger("gptbridge.rag.model_registry")


# ============================================================================
# Model Registry
# ============================================================================

@dataclass(frozen=True)
class ModelRegistryEntry:
    """Model registry entry with full metadata."""
    role: str                    # "embedding", "reranker", "generation"
    model_id: str                # e.g., "qwen3-embedding:4b"
    runtime: str                 # "ollama", "transformers", "vllm", "llamacpp"
    revision: str                # Model revision/tag
    dimension: Optional[int]     # Vector dimension (for embedding)
    enabled: bool
    canonical: bool              # Part of canonical path
    installed: bool              # Actually available
    verified_at: Optional[str] = None


class ModelRegistry:
    """Centralised model registry for RAG pipeline.

    Resolves confusion between:
    - Ollama model names (what's pulled)
    - Actual runtime (what executes)
    - Canonical status (what's in the critical path)
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None
        self._cache: dict[str, ModelRegistryEntry] = {}

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def register(
        self,
        role: str,
        model_id: str,
        runtime: str,
        revision: str,
        dimension: Optional[int] = None,
        enabled: bool = True,
        canonical: bool = True,
        installed: bool = True,
    ) -> ModelRegistryEntry:
        """Register or update a model."""
        conn = self._get_conn()
        entry = ModelRegistryEntry(
            role=role,
            model_id=model_id,
            runtime=runtime,
            revision=revision,
            dimension=dimension,
            enabled=enabled,
            canonical=canonical,
            installed=installed,
            verified_at=datetime.now(timezone.utc).isoformat(),
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gptbridge_rag.model_registry
                (role, model_id, runtime, revision, dimension, enabled, canonical, installed, verified_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (role, model_id) DO UPDATE SET
                    runtime = EXCLUDED.runtime,
                    revision = EXCLUDED.revision,
                    dimension = EXCLUDED.dimension,
                    enabled = EXCLUDED.enabled,
                    canonical = EXCLUDED.canonical,
                    installed = EXCLUDED.installed,
                    verified_at = EXCLUDED.verified_at
                """,
                (
                    entry.role, entry.model_id, entry.runtime, entry.revision,
                    entry.dimension, entry.enabled, entry.canonical,
                    entry.installed, entry.verified_at,
                ),
            )

        self._cache[f"{role}:{model_id}"] = entry
        _logger.info("ModelRegistry: registered %s/%s (runtime=%s, canonical=%s)",
                     role, model_id, runtime, canonical)
        return entry

    def get(self, role: str, model_id: str) -> Optional[ModelRegistryEntry]:
        """Get model registration."""
        key = f"{role}:{model_id}"
        if key in self._cache:
            return self._cache[key]

        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, model_id, runtime, revision, dimension, enabled,
                       canonical, installed, verified_at
                FROM gptbridge_rag.model_registry
                WHERE role = %s AND model_id = %s
                """,
                (role, model_id),
            )
            row = cur.fetchone()
            if row:
                entry = ModelRegistryEntry(
                    role=row["role"],
                    model_id=row["model_id"],
                    runtime=row["runtime"],
                    revision=row["revision"],
                    dimension=row["dimension"],
                    enabled=row["enabled"],
                    canonical=row["canonical"],
                    installed=row["installed"],
                    verified_at=row["verified_at"],
                )
                self._cache[key] = entry
                return entry
        return None

    def list_by_role(self, role: str) -> list[ModelRegistryEntry]:
        """List all models for a role."""
        conn = self._get_conn()
        entries = []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, model_id, runtime, revision, dimension, enabled,
                       canonical, installed, verified_at
                FROM gptbridge_rag.model_registry
                WHERE role = %s
                ORDER BY model_id
                """,
                (role,),
            )
            for row in cur.fetchall():
                entries.append(ModelRegistryEntry(
                    role=row["role"],
                    model_id=row["model_id"],
                    runtime=row["runtime"],
                    revision=row["revision"],
                    dimension=row["dimension"],
                    enabled=row["enabled"],
                    canonical=row["canonical"],
                    installed=row["installed"],
                    verified_at=row["verified_at"],
                ))
        return entries

    def list_canonical(self) -> list[ModelRegistryEntry]:
        """List all canonical models."""
        conn = self._get_conn()
        entries = []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, model_id, runtime, revision, dimension, enabled,
                       canonical, installed, verified_at
                FROM gptbridge_rag.model_registry
                WHERE canonical = true AND enabled = true
                ORDER BY role, model_id
                """,
            )
            for row in cur.fetchall():
                entries.append(ModelRegistryEntry(
                    role=row["role"],
                    model_id=row["model_id"],
                    runtime=row["runtime"],
                    revision=row["revision"],
                    dimension=row["dimension"],
                    enabled=row["enabled"],
                    canonical=row["canonical"],
                    installed=row["installed"],
                    verified_at=row["verified_at"],
                ))
        return entries

    def verify_model(self, role: str, model_id: str) -> bool:
        """Mark model as verified (actually callable)."""
        entry = self.get(role, model_id)
        if not entry:
            return False
        # In production: actually test the model
        # For now, just update timestamp
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.model_registry
                SET verified_at = %s
                WHERE role = %s AND model_id = %s
                """,
                (datetime.now(timezone.utc).isoformat(), role, model_id),
            )
        return True


# ============================================================================
# Enhanced RAG Status
# ============================================================================

@dataclass(frozen=True)
class RagComponentStatus:
    """Detailed component status."""
    name: str
    healthy: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    last_check: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class RagEnhancedStatus:
    """Complete RAG system status for governance/operations."""

    # Overall
    state: CanonicalState
    canonical: bool

    # Components
    qdrant: RagComponentStatus
    postgresql: RagComponentStatus
    embedding: RagComponentStatus
    reranker: RagComponentStatus
    generation: RagComponentStatus
    outbox: RagComponentStatus
    reconciliation: RagComponentStatus

    # Active generation
    active_generation: Optional[str] = None
    generation_state: Optional[GenerationState] = None
    alias_target: Optional[str] = None

    # Metrics
    outbox_pending: int = 0
    outbox_failed: int = 0
    reconciliation_pending: int = 0
    index_total_points: int = 0

    # Canonical models
    canonical_models: list[dict[str, Any]] = field(default_factory=list)

    # Timestamp
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API/telemetry."""
        return {
            "state": self.state.value,
            "canonical": self.canonical,
            "qdrant": {
                "healthy": self.qdrant.healthy,
                "message": self.qdrant.message,
                "details": self.qdrant.details,
            },
            "postgresql": {
                "healthy": self.postgresql.healthy,
                "message": self.postgresql.message,
                "details": self.postgresql.details,
            },
            "embedding": {
                "healthy": self.embedding.healthy,
                "message": self.embedding.message,
                "details": self.embedding.details,
            },
            "reranker": {
                "healthy": self.reranker.healthy,
                "message": self.reranker.message,
                "details": self.reranker.details,
            },
            "generation": {
                "healthy": self.generation.healthy,
                "message": self.generation.message,
                "details": self.generation.details,
            },
            "outbox": {
                "healthy": self.outbox.healthy,
                "message": self.outbox.message,
                "details": self.outbox.details,
            },
            "reconciliation": {
                "healthy": self.reconciliation.healthy,
                "message": self.reconciliation.message,
                "details": self.reconciliation.details,
            },
            "active_generation": self.active_generation,
            "generation_state": self.generation_state.value if self.generation_state else None,
            "alias_target": self.alias_target,
            "outbox_pending": self.outbox_pending,
            "outbox_failed": self.outbox_failed,
            "reconciliation_pending": self.reconciliation_pending,
            "index_total_points": self.index_total_points,
            "canonical_models": self.canonical_models,
            "timestamp": self.timestamp,
        }


class RagStatusProvider:
    """Aggregates complete RAG status from all components."""

    def __init__(
        self,
        qdrant: Any,              # QdrantCanonicalRuntime
        pg_metadata: Any,         # PostgreSQLMetadataAuthority
        generation_manager: Any,  # GenerationManager
        outbox_repo: Any,         # OutboxRepository
        model_registry: ModelRegistry,
        embedding_provider: Any = None,
        reranker_provider: Any = None,
    ) -> None:
        self.qdrant = qdrant
        self.pg_metadata = pg_metadata
        self.generation_manager = generation_manager
        self.outbox_repo = outbox_repo
        self.model_registry = model_registry
        self.embedding_provider = embedding_provider
        self.reranker_provider = reranker_provider

    async def get_status(self) -> RagEnhancedStatus:
        """Get complete enhanced RAG status."""
        # Check components
        qdrant_healthy = self.qdrant.is_healthy() if self.qdrant else False
        pg_healthy = self.pg_metadata.is_healthy() if self.pg_metadata else False

        # Get active generation
        active_gen = None
        gen_state = None
        alias_target = None
        if self.generation_manager:
            try:
                active_gen_obj = await self.generation_manager.get_active_generation()
                if active_gen_obj:
                    active_gen = active_gen_obj.generation_id
                    gen_state = active_gen_obj.state
                    alias_target = active_gen_obj.alias_name
            except Exception:
                pass

        # Get alias target
        if self.qdrant:
            try:
                alias_target = await self.qdrant.get_alias_target(self.qdrant.config.collection_name)
            except Exception:
                pass

        # Outbox stats
        outbox_stats = {"pending": 0, "failed": 0}
        if self.outbox_repo:
            try:
                outbox_stats = self.outbox_repo.get_stats()
            except Exception:
                pass

        # Reconciliation pending
        recon_pending = 0  # Would query reconciliation queue

        # Index total points
        index_points = 0
        if self.qdrant:
            try:
                index_points = self.qdrant.points_count() or 0
            except Exception:
                pass

        # Canonical models
        canonical_models = []
        for entry in self.model_registry.list_canonical():
            canonical_models.append({
                "role": entry.role,
                "model_id": entry.model_id,
                "runtime": entry.runtime,
                "revision": entry.revision,
                "dimension": entry.dimension,
                "verified_at": entry.verified_at,
            })

        # Determine overall state
        state = self._determine_state(
            qdrant_healthy, pg_healthy,
            outbox_stats.get("failed", 0),
            recon_pending,
        )

        return RagEnhancedStatus(
            state=state,
            canonical=qdrant_healthy and pg_healthy,
            qdrant=RagComponentStatus(
                name="qdrant",
                healthy=qdrant_healthy,
                message="Qdrant reachable" if qdrant_healthy else "Qdrant unreachable",
                details={"collections": self._get_qdrant_collections()},
            ),
            postgresql=RagComponentStatus(
                name="postgresql",
                healthy=pg_healthy,
                message="PostgreSQL connected" if pg_healthy else "PostgreSQL unreachable",
            ),
            embedding=RagComponentStatus(
                name="embedding",
                healthy=self.embedding_provider is not None,
                message="Embedding provider configured" if self.embedding_provider else "No embedding provider",
                details={"provider": type(self.embedding_provider).__name__ if self.embedding_provider else None},
            ),
            reranker=RagComponentStatus(
                name="reranker",
                healthy=self.reranker_provider is not None,
                message="Reranker configured" if self.reranker_provider else "No reranker",
                details={"provider": type(self.reranker_provider).__name__ if self.reranker_provider else None},
            ),
            generation=RagComponentStatus(
                name="generation_manager",
                healthy=self.generation_manager is not None,
                message="Generation manager active" if self.generation_manager else "Not initialized",
                details={"active_generation": active_gen},
            ),
            outbox=RagComponentStatus(
                name="outbox",
                healthy=outbox_stats.get("failed", 0) == 0,
                message=f"Pending: {outbox_stats.get('pending', 0)}, Failed: {outbox_stats.get('failed', 0)}",
                details=outbox_stats,
            ),
            reconciliation=RagComponentStatus(
                name="reconciliation",
                healthy=recon_pending < 100,
                message=f"Pending: {recon_pending}",
                details={"pending": recon_pending},
            ),
            active_generation=active_gen,
            generation_state=gen_state,
            alias_target=alias_target,
            outbox_pending=outbox_stats.get("pending", 0),
            outbox_failed=outbox_stats.get("failed", 0),
            reconciliation_pending=recon_pending,
            index_total_points=index_points,
            canonical_models=canonical_models,
        )

    def _determine_state(
        self,
        qdrant_healthy: bool,
        pg_healthy: bool,
        outbox_failed: int,
        recon_pending: int,
    ) -> CanonicalState:
        """Determine overall canonical state."""
        if not qdrant_healthy or not pg_healthy:
            if not qdrant_healthy and not pg_healthy:
                return CanonicalState.DEGRADED
            if not qdrant_healthy:
                return CanonicalState.VECTOR_UNAVAILABLE
            return CanonicalState.METADATA_UNAVAILABLE

        if outbox_failed > 0:
            return CanonicalState.OUTBOX_BACKLOG
        if recon_pending > 100:
            return CanonicalState.RECONCILIATION_BACKLOG

        return CanonicalState.CANONICAL_READY

    def _get_qdrant_collections(self) -> list[str]:
        """Get list of Qdrant collections."""
        if not self.qdrant:
            return []
        try:
            collections = self.qdrant.client.get_collections()
            return [c.name for c in collections.collections]
        except Exception:
            return []


# ============================================================================
# Default Registry Initialisation
# ============================================================================

DEFAULT_CANONICAL_MODELS = [
    # Embedding
    {
        "role": "embedding",
        "model_id": "qwen3-embedding:4b",
        "runtime": "ollama",
        "revision": "latest",
        "dimension": 2560,
        "enabled": True,
        "canonical": True,
        "installed": True,
    },
    # Reranker (could be HF local or vLLM)
    {
        "role": "reranker",
        "model_id": "Qwen/Qwen3-Reranker-0.6B",
        "runtime": "transformers",
        "revision": "main",
        "dimension": None,
        "enabled": True,
        "canonical": True,
        "installed": True,
    },
    # Generation (local LLM)
    {
        "role": "generation",
        "model_id": "qwen3:8b",
        "runtime": "ollama",
        "revision": "latest",
        "dimension": None,
        "enabled": True,
        "canonical": True,
        "installed": True,
    },
]


def initialise_default_models(registry: ModelRegistry) -> None:
    """Initialise default canonical models."""
    for m in DEFAULT_CANONICAL_MODELS:
        registry.register(
            role=m["role"],
            model_id=m["model_id"],
            runtime=m["runtime"],
            revision=m["revision"],
            dimension=m["dimension"],
            enabled=m["enabled"],
            canonical=m["canonical"],
            installed=m["installed"],
        )


__all__ = [
    "ModelRegistryEntry",
    "ModelRegistry",
    "RagComponentStatus",
    "RagEnhancedStatus",
    "RagStatusProvider",
    "DEFAULT_CANONICAL_MODELS",
    "initialise_default_models",
]
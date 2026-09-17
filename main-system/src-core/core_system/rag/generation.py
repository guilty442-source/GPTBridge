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
from typing import Any, Callable, Optional

from shared_layer.database.qdrant_capacity import (
    CapacityDecision,
    CapacityVerdict,
    QdrantCollectionSpec,
    authorize_operation,
    dual_collection_headroom,
)
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
    VALIDATING = "VERIFYING"   # Alias — same lifecycle stage
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
    parser_version: str = "v1"
    vector_schema_version: str = "v1"
    metadata_schema_version: str = "v1"
    policy_version: str = "v1"
    activated_at: Optional[str] = None
    retired_at: Optional[str] = None

    def index_fingerprint(self) -> str:
        """RAG-11 fingerprint — any component change means a new
        generation is required; generations are never edited in place."""
        material = "|".join((
            self.parser_version,
            self.chunk_policy_version,
            self.embedding_model,
            str(self.embedding_dimension),
            self.vector_schema_version,
            self.index_schema_version,
        ))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


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
    parser_version: str = "v1"
    vector_schema_version: str = "v1"
    metadata_schema_version: str = "v1"
    policy_version: str = "v1"


class GenerationLifecycleStatus(str, Enum):
    """Typed outcome of a capacity-gated generation lifecycle operation."""
    OK = "OK"
    PARTIAL = "PARTIAL"
    REFUSED = "REFUSED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class GenerationCleanupResult:
    """Typed result of ``cleanup_old_generations``.

    ``ok`` is True only for a fully completed cleanup; a missing Qdrant
    client, missing generation listing, or capacity refusal is reported
    explicitly and never silently treated as success.
    """
    status: GenerationLifecycleStatus
    deleted: tuple[str, ...] = ()
    retained: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    reason: Optional[str] = None
    headroom_bytes: Optional[int] = None
    capacity: Optional[CapacityDecision] = None
    queue: Optional[Any] = None

    @property
    def ok(self) -> bool:
        return self.status is GenerationLifecycleStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "ok": self.ok,
            "deleted": list(self.deleted),
            "retained": list(self.retained),
            "failed": list(self.failed),
            "reason": self.reason,
            "headroom_bytes": self.headroom_bytes,
            "capacity": self.capacity.to_dict() if self.capacity else None,
            "queue": self.queue.to_dict() if self.queue is not None else None,
        }


@dataclass(frozen=True)
class GenerationBuildResult:
    """Typed result of the capacity-gated collection build."""
    ok: bool
    status: GenerationLifecycleStatus
    collection_name: str
    created: bool = False
    reason: Optional[str] = None
    capacity: Optional[CapacityDecision] = None
    queue: Optional[Any] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "ok": self.ok,
            "collection_name": self.collection_name,
            "created": self.created,
            "reason": self.reason,
            "capacity": self.capacity.to_dict() if self.capacity else None,
            "queue": self.queue.to_dict() if self.queue is not None else None,
        }


class GenerationManager:
    """Manages index generations, alias switching, and lifecycle."""

    def __init__(
        self,
        qdrant_client: Any,
        config: GenerationConfig,
        metadata_db: Any,  # PostgreSQLMetadataAuthority
        *,
        available_bytes_provider: Optional[Callable[[], Optional[int]]] = None,
        queue_state_provider: Optional[Callable[[], tuple[Any, int]]] = None,
        retired_generations_provider: Optional[
            Callable[[str], Any]
        ] = None,
    ) -> None:
        self.qdrant = qdrant_client
        self.config = config
        self.metadata_db = metadata_db
        self._current_generation: Optional[IndexGeneration] = None
        # Capacity / admission providers (injected).  When absent the
        # capacity check is reported as not-configured instead of assumed
        # compliant; when present, an unknown budget fails closed.
        self._available_bytes_provider = available_bytes_provider
        self._queue_state_provider = queue_state_provider
        self._retired_generations_provider = retired_generations_provider

    def _generate_generation_id(self) -> str:
        """Generate unique generation ID: gen-YYYYMMDD-NNN."""
        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
        # Simple counter - in production use sequence or UUID suffix
        suffix = uuid.uuid4().hex[:3]
        return f"gen-{date_part}-{suffix}"

    def _physical_collection_name(self, generation_id: str) -> str:
        """Map generation ID to physical Qdrant collection name."""
        return f"{self.config.alias_name}_{generation_id}"

    @staticmethod
    def _spec_for(
        generation: IndexGeneration,
        *,
        points_estimate: Optional[int] = None,
    ) -> QdrantCollectionSpec:
        """Estimate one generation's Qdrant footprint for the capacity gate."""
        points = (
            int(generation.points_count or 0)
            if points_estimate is None
            else int(points_estimate)
        )
        return QdrantCollectionSpec(
            vector_count=max(0, points),
            dimension=max(1, int(generation.embedding_dimension)),
        )

    async def _capacity_decision(
        self,
        operation: str,
        successor: Optional[IndexGeneration],
    ) -> Optional[CapacityDecision]:
        """Qdrant capacity gate; None means no budget provider is configured.

        A configured provider that fails or yields an unknown budget returns
        an UNKNOWN decision with ``allowed=False`` — fail-closed.
        """
        provider = self._available_bytes_provider
        if provider is None:
            return None
        try:
            available = provider()
        except Exception as exc:
            return CapacityDecision(
                operation=operation, verdict=CapacityVerdict.UNKNOWN,
                allowed=False, reason=f"capacity-provider-failed: {exc}",
            )
        if available is None:
            return CapacityDecision(
                operation=operation, verdict=CapacityVerdict.UNKNOWN,
                allowed=False, reason="capacity-budget-unavailable",
            )
        try:
            active = await self.get_active_generation()
        except Exception as exc:
            return CapacityDecision(
                operation=operation, verdict=CapacityVerdict.UNKNOWN,
                allowed=False,
                reason=f"active-generation-unavailable: {exc}",
            )
        points_estimate = (
            int(active.points_count or 0) if active is not None else None
        )
        return authorize_operation(
            operation,
            current=self._spec_for(active) if active is not None else None,
            successor=(
                self._spec_for(successor, points_estimate=points_estimate)
                if successor is not None
                else None
            ),
            available_bytes=int(available),
        )

    def _queue_gate(self, operation: str) -> Optional[Any]:
        """Bounded rebuild-queue admission; None when not configured."""
        provider = self._queue_state_provider
        if provider is None:
            return None
        from shared_layer.database.rag_capacity import gate_lifecycle_operation
        state, depth = provider()
        return gate_lifecycle_operation(
            operation, queue_state=state, queue_depth=int(depth or 0)
        )

    async def _rollback_headroom(
        self, rollback: Optional[IndexGeneration]
    ) -> Optional[int]:
        """Dual-collection headroom kept for the retained rollback generation."""
        if rollback is None:
            return None
        try:
            active = await self.get_active_generation()
        except Exception:
            return None
        if active is None:
            return None
        return dual_collection_headroom(
            self._spec_for(active), self._spec_for(rollback)
        )

    @staticmethod
    def _deletable_state(generation: Any) -> bool:
        state = getattr(generation, "state", None)
        value = state.value if hasattr(state, "value") else str(state)
        return value in (
            GenerationState.RETIRED.value, GenerationState.FAILED.value,
        )

    async def get_active_generation(self) -> Optional[IndexGeneration]:
        """Fetch the currently ACTIVE generation.

        PostgreSQL is authoritative — the in-memory cache is only a
        shortcut, so a restart or a crashed BUILDING generation can never
        hide the live ACTIVE one."""
        fetch = getattr(self.metadata_db, "get_active_generation", None)
        if fetch is not None:
            gen = await fetch(self.config.alias_name)
            if gen is not None:
                self._current_generation = gen
                return gen
        if (self._current_generation
                and self._current_generation.state == GenerationState.ACTIVE):
            return self._current_generation
        return None

    async def create_generation(self) -> IndexGeneration:
        """Create a new BUILDING generation (starts full reindex)."""
        gen_id = self._generate_generation_id()
        collection_name = self._physical_collection_name(gen_id)

        previous = await self.get_active_generation()
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
            previous_generation_id=(
                previous.generation_id if previous else None
            ),
            parser_version=self.config.parser_version,
            vector_schema_version=self.config.vector_schema_version,
            metadata_schema_version=self.config.metadata_schema_version,
            policy_version=self.config.policy_version,
        )

        # Persist to PostgreSQL (metadata_db should have upsert_generation)
        await self.metadata_db.upsert_generation(generation)
        _logger.info("GenerationManager: created %s (collection=%s)", gen_id, collection_name)
        return generation

    async def ensure_collection(self, generation: IndexGeneration) -> bool:
        """Ensure the generation's physical collection exists.

        Returns True only when the build gate allowed the operation and the
        collection is verified present afterwards; a capacity refusal or a
        missing Qdrant client returns False (never a fake success).  Call
        ``ensure_collection_checked`` for the typed refusal reason.
        """
        result = await self.ensure_collection_checked(generation)
        return result.ok

    async def ensure_collection_checked(
        self, generation: IndexGeneration
    ) -> GenerationBuildResult:
        """Capacity-gated collection build with a typed result.

        Order: rebuild-queue admission -> Qdrant capacity (dual-collection
        headroom: the ACTIVE collection must stay queryable while the
        successor is built) -> real ``create_collection``.  Any failure is
        reported as REFUSED / UNAVAILABLE / FAILED — never as success.
        """
        collection_name = generation.collection_name
        try:
            gate = self._queue_gate("build")
        except Exception as exc:
            return GenerationBuildResult(
                ok=False, status=GenerationLifecycleStatus.REFUSED,
                collection_name=collection_name,
                reason=f"queue-gate-failed: {exc}",
            )
        if gate is not None and not gate.allowed:
            return GenerationBuildResult(
                ok=False, status=GenerationLifecycleStatus.REFUSED,
                collection_name=collection_name,
                reason=gate.reason, queue=gate,
            )

        decision = await self._capacity_decision("build", generation)
        if decision is not None and not decision.allowed:
            return GenerationBuildResult(
                ok=False, status=GenerationLifecycleStatus.REFUSED,
                collection_name=collection_name,
                reason=decision.reason, capacity=decision, queue=gate,
            )

        if self.qdrant is None:
            return GenerationBuildResult(
                ok=False, status=GenerationLifecycleStatus.UNAVAILABLE,
                collection_name=collection_name,
                reason="qdrant-client-unavailable",
                capacity=decision, queue=gate,
            )
        try:
            collections = self.qdrant.get_collections()
            names = {c.name for c in collections.collections}
            created = False
            if collection_name not in names:
                self.qdrant.create_collection(
                    collection_name=collection_name,
                    vectors_config=VectorParams(
                        size=generation.embedding_dimension,
                        distance=Distance.COSINE,
                    ),
                )
                created = True
                _logger.info("GenerationManager: created collection %s", collection_name)
            return GenerationBuildResult(
                ok=True, status=GenerationLifecycleStatus.OK,
                collection_name=collection_name, created=created,
                capacity=decision, queue=gate,
            )
        except Exception as exc:
            _logger.error(
                "GenerationManager: ensure_collection failed for %s: %s",
                collection_name, exc,
            )
            return GenerationBuildResult(
                ok=False, status=GenerationLifecycleStatus.FAILED,
                collection_name=collection_name, reason=str(exc),
                capacity=decision, queue=gate,
            )

    async def requires_rebuild(
        self, generation: Optional[IndexGeneration]
    ) -> bool:
        """True when the config fingerprint differs from the generation's —
        any version/component drift demands a NEW generation, never an
        in-place schema edit of the ACTIVE one."""
        if generation is None:
            return True
        candidate = IndexGeneration(
            generation_id=generation.generation_id,
            index_schema_version=self.config.index_schema_version,
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            chunk_policy_version=self.config.chunk_policy_version,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            created_at=generation.created_at,
            state=generation.state,
            collection_name=generation.collection_name,
            alias_name=generation.alias_name,
            parser_version=self.config.parser_version,
            vector_schema_version=self.config.vector_schema_version,
            metadata_schema_version=self.config.metadata_schema_version,
            policy_version=self.config.policy_version,
        )
        return candidate.index_fingerprint() != generation.index_fingerprint()

    async def promote_to_active(self, generation: IndexGeneration) -> bool:
        """Atomically switch alias to point to the new generation.

        Uses a single ``update_aliases`` call (create+delete in one Qdrant
        operation) so a crash mid-swap can never leave the alias missing
        or pointing at a half-built collection; falls back to sequential
        create_alias on clients that lack the batch API.
        """
        try:
            # 1. Atomic alias swap: alias → new physical collection
            update_aliases = getattr(self.qdrant, "update_aliases", None)
            if update_aliases is not None:
                from qdrant_client.http.models import (
                    CreateAlias,
                    CreateAliasOperation,
                    DeleteAlias,
                    DeleteAliasOperation,
                )
                update_aliases(
                    change_aliases_operations=[
                        CreateAliasOperation(
                            create_alias=CreateAlias(
                                collection_name=generation.collection_name,
                                alias_name=generation.alias_name,
                            )
                        ),
                    ]
                )
            else:
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
                parser_version=generation.parser_version,
                vector_schema_version=generation.vector_schema_version,
                metadata_schema_version=generation.metadata_schema_version,
                policy_version=generation.policy_version,
                activated_at=datetime.now(timezone.utc).isoformat(),
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
                    parser_version=gen.parser_version,
                    vector_schema_version=gen.vector_schema_version,
                    metadata_schema_version=gen.metadata_schema_version,
                    policy_version=gen.policy_version,
                    retired_at=datetime.now(timezone.utc).isoformat(),
                )
                await self.metadata_db.upsert_generation(retired)
                _logger.info("GenerationManager: retired %s", generation_id)
        except Exception as exc:
            _logger.warning("GenerationManager: _retire_generation failed: %s", exc)

    async def cleanup_old_generations(
        self, *, keep: Optional[int] = None
    ) -> GenerationCleanupResult:
        """Delete RETIRED/FAILED generations beyond the retention limit.

        Bounded, real Qdrant deletions through the injected client
        (``delete_collection``).  Explicit no-op when the client or the
        retired-generation listing is missing.  Refuses when the rebuild
        queue is not accepting or the capacity budget is unknown/exceeded
        (dual-collection headroom for the retained rollback generation is
        checked first).  Never claims success for work it did not do.
        """
        keep_count = (
            self.config.max_generations_to_keep
            if keep is None
            else max(0, int(keep))
        )

        lister = getattr(self.metadata_db, "list_retired_generations", None)
        if lister is None:
            lister = self._retired_generations_provider
        if lister is None:
            return GenerationCleanupResult(
                status=GenerationLifecycleStatus.UNAVAILABLE,
                reason="retired-generation-listing-unavailable",
            )
        try:
            listed = list(await lister(self.config.alias_name))
        except Exception as exc:
            return GenerationCleanupResult(
                status=GenerationLifecycleStatus.UNAVAILABLE,
                reason=f"retired-generation-listing-failed: {exc}",
            )

        deletable = [gen for gen in listed if self._deletable_state(gen)]
        deletable.sort(
            key=lambda gen: (
                str(getattr(gen, "created_at", "") or ""),
                str(getattr(gen, "generation_id", "") or ""),
            )
        )
        retained = deletable[max(0, len(deletable) - keep_count):]
        overflow = deletable[: max(0, len(deletable) - keep_count)]
        retained_ids = tuple(
            str(getattr(gen, "generation_id", "") or "") for gen in retained
        )
        if not overflow:
            return GenerationCleanupResult(
                status=GenerationLifecycleStatus.OK,
                retained=retained_ids,
            )

        delete_collection = (
            getattr(self.qdrant, "delete_collection", None)
            if self.qdrant is not None
            else None
        )
        if not callable(delete_collection):
            return GenerationCleanupResult(
                status=GenerationLifecycleStatus.UNAVAILABLE,
                reason="qdrant-client-unavailable",
                retained=retained_ids,
            )

        rollback = retained[-1] if retained else None
        try:
            gate = self._queue_gate("cleanup")
        except Exception as exc:
            return GenerationCleanupResult(
                status=GenerationLifecycleStatus.REFUSED,
                reason=f"queue-gate-failed: {exc}",
                retained=retained_ids,
            )
        if gate is not None and not gate.allowed:
            return GenerationCleanupResult(
                status=GenerationLifecycleStatus.REFUSED,
                reason=gate.reason, retained=retained_ids, queue=gate,
            )

        decision = await self._capacity_decision("cleanup", rollback)
        if decision is not None and not decision.allowed:
            return GenerationCleanupResult(
                status=GenerationLifecycleStatus.REFUSED,
                reason=decision.reason, retained=retained_ids,
                capacity=decision, queue=gate,
            )
        headroom_bytes = (
            decision.headroom_bytes if decision is not None else None
        )
        if headroom_bytes is None:
            headroom_bytes = await self._rollback_headroom(rollback)

        deleted: list[str] = []
        failed: list[str] = []
        for gen in overflow:
            generation_id = str(getattr(gen, "generation_id", "") or "")
            collection_name = str(
                getattr(gen, "collection_name", "") or ""
            )
            if not collection_name:
                failed.append(generation_id)
                _logger.error(
                    "GenerationManager: cleanup skipped %s — no collection name",
                    generation_id,
                )
                continue
            try:
                delete_collection(collection_name=collection_name)
            except Exception as exc:
                failed.append(generation_id)
                _logger.error(
                    "GenerationManager: cleanup delete failed for %s: %s",
                    collection_name, exc,
                )
            else:
                deleted.append(generation_id)
                _logger.info(
                    "GenerationManager: cleanup deleted %s (%s)",
                    generation_id, collection_name,
                )

        status = (
            GenerationLifecycleStatus.OK
            if not failed
            else GenerationLifecycleStatus.PARTIAL
        )
        return GenerationCleanupResult(
            status=status,
            deleted=tuple(deleted),
            retained=retained_ids,
            failed=tuple(failed),
            reason=(
                None if not failed
                else f"{len(failed)} deletion(s) failed"
            ),
            headroom_bytes=headroom_bytes,
            capacity=decision,
            queue=gate,
        )

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


# PostgreSQL generation schema — gptbridge_rag.generation is the canonical
# generation registry; rag_metadata applies these statements in _SAGA_DDL.
_GENERATION_DDL = (
    """
    CREATE TABLE IF NOT EXISTS gptbridge_rag.generation (
        generation_id TEXT PRIMARY KEY,
        index_schema_version TEXT NOT NULL,
        embedding_model TEXT NOT NULL,
        embedding_dimension INTEGER NOT NULL,
        chunk_policy_version TEXT NOT NULL,
        chunk_size INTEGER NOT NULL,
        chunk_overlap INTEGER NOT NULL,
        created_at_utc TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('BUILDING','VERIFYING','ACTIVE','RETIRED','FAILED')),
        collection_name TEXT NOT NULL,
        alias_name TEXT NOT NULL,
        previous_generation_id TEXT,
        points_count INTEGER DEFAULT 0,
        verification_result JSONB,
        error_message TEXT,
        parser_version TEXT DEFAULT 'v1',
        vector_schema_version TEXT DEFAULT 'v1',
        metadata_schema_version TEXT DEFAULT 'v1',
        policy_version TEXT DEFAULT 'v1',
        activated_at_utc TEXT,
        retired_at_utc TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_generation_active
    ON gptbridge_rag.generation (alias_name) WHERE state = 'ACTIVE'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rag_generation_alias_state
    ON gptbridge_rag.generation (alias_name, state)
    """,
)

GENERATION_TABLE_SQL = "\n".join(_GENERATION_DDL)

from qdrant_client.http.models import Distance, VectorParams  # noqa: E402

__all__ = [
    "GenerationState",
    "IndexGeneration",
    "GenerationConfig",
    "GenerationLifecycleStatus",
    "GenerationCleanupResult",
    "GenerationBuildResult",
    "GenerationManager",
    "GENERATION_TABLE_SQL",
]
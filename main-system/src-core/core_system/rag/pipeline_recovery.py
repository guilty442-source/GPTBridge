"""A374 recovery + reconciliation data flow for CanonicalRagPipeline.

DEGRADED → RECONCILING → CANONICAL is only allowed after pending
``pending_rag_mutation`` items are replayed through the real data flow —

    pending mutation
      → re-fetch the owning module's original content
      → re-chunk (canonical chunker contract)
      → re-embed via the governed local runtime (qwen3-embedding:4b, 2560d)
      → Qdrant upsert / delete
      → PostgreSQL metadata / index_state
      → verify index_state
      → mark reconciled (canonical_synced_at)

— followed by honest parity (index_state chunk coverage vs Qdrant
points, point-id / content-hash / embedding-version completeness) plus
queue-drain verification.  Degraded SQLite hashing vectors are NEVER
replayed into the 2560-dim canonical collection; embeddings are always
regenerated.  A failed attempt stays bounded at DEGRADED and is surfaced
as the derived RECONCILIATION_FAILED state.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .runtime_state import (
    CanonicalCheckError,
    QueueOperation,
    QueueStatus,
    RagRuntimeState,
    TransitionError,
)

_logger = logging.getLogger("gptbridge.rag")

_POINT_NAMESPACE = uuid.UUID("a374b1c0-9e2f-4d6a-8c3e-5f7a9b1d2e4f")
_MAX_ATTEMPTS = 8
_RETRY_BASE_SECONDS = 5.0


class PipelineRecoveryMixin:
    """Recovery driver + reconciliation replay + health reporting."""

    # -- recovery driver ----------------------------------------------------

    async def attempt_recovery(self) -> RagRuntimeState:
        """A374 recovery: DEGRADED → RECONCILING → CANONICAL only after
        queue replay + parity + queue-drain verification; failure stays
        bounded at DEGRADED and is surfaced as RECONCILIATION_FAILED.
        No-op outside DEGRADED or while canonical services are down."""
        if self._blocked_reason:
            # Hard contract violation (dimension mismatch, non-loopback URL,
            # unverifiable contract): surface BLOCKED, never self-heal into
            # RECONCILING/CANONICAL on service health alone.
            return self.state
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
            await self.run_reconciliation()
            parity = await self._recovery_parity()
        except Exception as exc:
            return self._state_machine.fail_reconciliation(str(exc))
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

    # -- pending_rag_mutation recording ------------------------------------

    async def _enqueue_degraded_write(
        self,
        *,
        module_id: str,
        resource_id: str,
        locator_id: str,
        source_revision: int,
        content_hash: str,
        operation: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record a pending_rag_mutation locally + mirror to the canonical
        PostgreSQL queue when it is reachable (durable both ways)."""
        item = self._state_machine.enqueue_degraded_mutation(
            module_id=module_id,
            resource_id=resource_id,
            locator_id=locator_id,
            source_revision=source_revision,
            content_hash=content_hash,
            operation=operation,
            embedding_model=self.config.embedding_model,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            payload={"module_id": module_id, **(payload or {})},
        )
        try:
            await self.postgresql.enqueue_reconciliation(item)
        except Exception as exc:
            _logger.warning(
                "CanonicalRagPipeline: PG queue mirror failed for %s:%s: %s",
                module_id, resource_id, exc,
            )

    async def _enqueue_document_mutation(
        self, document: dict[str, Any], chunks: list[dict[str, Any]]
    ) -> None:
        """Queue a durable pending_rag_mutation for a document write."""
        module_id = str(document["module_id"])
        resource_id = str(document["resource_id"])
        content_hash = str(
            document.get("sha256")
            or hashlib.sha256(
                "".join(str(c.get("content") or "") for c in chunks).encode("utf-8")
            ).hexdigest()
        )
        try:
            await self._enqueue_degraded_write(
                module_id=module_id,
                resource_id=resource_id,
                locator_id=str(
                    document.get("locator_id") or f"{module_id}:{resource_id}"
                ),
                source_revision=int(document.get("version") or 1),
                content_hash=content_hash,
                operation="update",
                payload={"title": document.get("title", "")},
            )
        except Exception as exc:
            _logger.warning(
                "CanonicalRagPipeline: mutation enqueue failed for %s:%s: %s",
                module_id, resource_id, exc,
            )

    # -- pending_rag_mutation replay ---------------------------------------

    async def run_reconciliation(self, *, batch: int = 25) -> dict[str, int]:
        """Replay durable pending mutations through the canonical write flow."""
        items = self._queue.lease(limit=batch)
        stats = {
            "leased": len(items), "reconciled": 0,
            "retried": 0, "dead_lettered": 0,
        }
        for item in items:
            try:
                await self._reconcile_item(item)
            except Exception as exc:
                bucket = await self._recon_failure(item, exc)
                stats[bucket] += 1
            else:
                self._queue.mark_verified(item.operation_id)
                await self.postgresql.mark_reconciled(item.operation_id)
                stats["reconciled"] += 1
        self._queue.drain_verified()
        await self.postgresql.drain_reconciled()
        _logger.info("CanonicalRagPipeline: reconciliation run %s", stats)
        return stats

    async def _recon_failure(self, item: Any, exc: Exception) -> str:
        """Schedule retry or dead-letter; returns the stats bucket name."""
        delay = min(300.0, _RETRY_BASE_SECONDS * (2 ** max(0, item.attempts - 1)))
        result = self._queue.mark_retry(
            item.operation_id, str(exc),
            delay_seconds=delay, max_attempts=_MAX_ATTEMPTS,
        )
        if result == QueueStatus.DEAD_LETTER.value:
            await self.postgresql.dead_letter_reconciliation(
                item.operation_id, str(exc)
            )
            _logger.error(
                "CanonicalRagPipeline: dead-lettered %s:%s (%s): %s",
                item.payload.get("module_id"), item.resource_id,
                item.operation, exc,
            )
            return "dead_lettered"
        await self.postgresql.mark_recon_retry(
            item.operation_id, str(exc),
            datetime.now(timezone.utc).isoformat(),
        )
        return "retried"

    async def _reconcile_item(self, item: Any) -> None:
        """Replay one pending mutation: re-fetch → re-chunk → re-embed →
        Qdrant upsert/delete → PG index_state → verify."""
        module_id = str(
            item.payload.get("module_id")
            or str(item.locator_id).split(":", 1)[0]
        )
        if item.operation in (
            QueueOperation.TOMBSTONE.value,
            QueueOperation.ARCHIVE.value,
        ):
            await self._reconcile_delete(item, module_id)
            return
        document, chunks, vectors = await self._rebuild_document(
            item, module_id
        )
        if not await self.index_document(
            document=document, chunks=chunks, vectors=vectors
        ):
            raise CanonicalCheckError(
                f"canonical rewrite rejected for {module_id}:{item.resource_id}"
            )
        state = await self.postgresql.get_index_state(module_id, item.resource_id)
        if state is None:
            raise CanonicalCheckError(
                f"index_state verification failed for {module_id}:{item.resource_id}"
            )

    async def _reconcile_delete(self, item: Any, module_id: str) -> None:
        """Tombstone/archive replay: remove vectors, raise authoritative tombstone.

        When a ``DeletionCoordinatorRuntime`` is injected
        (``self._deletion_coordinator``) the replay goes through its typed
        runtime entry, which keeps the fixed order (Qdrant delete before the
        PostgreSQL tombstone) and is idempotent.  Without it the canonical
        pipeline performs the same two steps directly.
        """
        coordinator = getattr(self, "_deletion_coordinator", None)
        if coordinator is not None:
            outcome = await coordinator.reconcile_tombstone_async(
                module_id=module_id,
                resource_id=item.resource_id,
                source_revision=item.source_revision,
                content_hash=item.content_hash,
                reason=f"reconciled-{item.operation}",
            )
            if not outcome.ok:
                raise CanonicalCheckError(
                    "deletion coordinator refused "
                    f"{module_id}:{item.resource_id}: {outcome.reason}"
                )
            return
        await self.qdrant.delete_resource(module_id, item.resource_id)
        await self.postgresql.raise_tombstone(
            module_id=module_id,
            resource_id=item.resource_id,
            source_revision=item.source_revision,
            content_hash=item.content_hash,
            reason=f"reconciled-{item.operation}",
        )

    async def _rebuild_document(
        self, item: Any, module_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[list[float]]]:
        """Re-fetch source content, re-chunk, re-embed (never replay vectors)."""
        if self._document_fetcher is None or self._embed_texts is None:
            raise CanonicalCheckError(
                "reconciler requires document_fetcher + embed_texts"
            )
        doc = await self._call_maybe_async(
            self._document_fetcher, module_id, item.locator_id
        )
        content = str((doc or {}).get("content") or "")
        if not content:
            raise CanonicalCheckError(
                f"source content unavailable for {module_id}:{item.resource_id}"
            )
        texts = self._rechunk(content)
        vectors = await self._call_maybe_async(self._embed_texts, texts)
        if len(vectors) != len(texts):
            raise CanonicalCheckError("RAG_EMBEDDING_COUNT_MISMATCH")
        for vector in vectors:
            if len(vector) != self.config.embedding_dimension:
                raise CanonicalCheckError(
                    f"EMBEDDING_DIMENSION_MISMATCH: {len(vector)}-dim vector "
                    f"cannot enter the {self.config.embedding_dimension}-dim "
                    "canonical collection — degraded vectors are never replayed"
                )
        return self._reconcile_records(item, module_id, doc, texts), \
            self._reconcile_chunks(item, module_id, doc, texts), vectors

    # -- record builders ----------------------------------------------------

    def _reconcile_records(
        self, item: Any, module_id: str, doc: dict[str, Any], texts: list[str]
    ) -> dict[str, Any]:
        return {
            "module_id": module_id,
            "resource_id": item.resource_id,
            "document_id": doc.get("document_id") or item.resource_id,
            "locator_id": item.locator_id,
            "owner_id": module_id,
            "platform_id": "",
            "data_category": "knowledge",
            "resource_type": "document",
            "resource_label": doc.get("title") or item.resource_id,
            "classification": "shared",
            "title": doc.get("title", ""),
            "source": doc.get("source", ""),
            "sha256": item.content_hash
            or hashlib.sha256("".join(texts).encode("utf-8")).hexdigest(),
            "version": item.source_revision,
            "character_count": sum(len(t) for t in texts),
            "chunk_count": len(texts),
            "embedding_model": self.config.embedding_model,
        }

    def _reconcile_chunks(
        self, item: Any, module_id: str, doc: dict[str, Any], texts: list[str]
    ) -> list[dict[str, Any]]:
        step = max(1, self.config.chunk_size - self.config.chunk_overlap)
        chunks: list[dict[str, Any]] = []
        for sequence, text in enumerate(texts):
            chunk_id = f"{item.resource_id}-recon-{sequence}"
            point_id = str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "sequence": sequence,
                    "character_start": sequence * step,
                    "character_end": sequence * step + len(text),
                    "content": text,
                    "resource_label": doc.get("title") or item.resource_id,
                    "source": doc.get("source", ""),
                    "title": doc.get("title", ""),
                    "point_id": point_id,
                    "qdrant_point_id": point_id,
                    "locator_fragment": f"#chunk-{sequence}",
                    "payload": {
                        "resource_id": item.resource_id,
                        "module_id": module_id,
                        "content": text,
                        "content_hash": hashlib.sha256(
                            text.encode("utf-8")
                        ).hexdigest(),
                        "reconciled_from": item.operation_id,
                    },
                }
            )
        return chunks

    # -- helpers ------------------------------------------------------------

    def _rechunk(self, content: str) -> list[str]:
        """Canonical chunker contract: fixed window + overlap."""
        size, overlap = self.config.chunk_size, self.config.chunk_overlap
        step = max(1, size - overlap)
        if len(content) <= size:
            return [content]
        return [content[i : i + size] for i in range(0, len(content), step)]

    @staticmethod
    async def _call_maybe_async(func: Any, *args: Any) -> Any:
        if inspect.iscoroutinefunction(func):
            return await func(*args)
        return await asyncio.to_thread(func, *args)

    # -- health surface ------------------------------------------------------

    async def health_check(self) -> dict[str, Any]:
        """Health check for all components (A374)."""
        await self.attempt_recovery()
        state = self._state_machine.state
        is_canonical = state == RagRuntimeState.CANONICAL
        result = {
            "pipeline_ready": self.is_ready(),
            "state": state.value,
            "effective_state": self._state_machine.effective_state,
            "gateway_state": self.gateway_state(),
            "blocked": self._blocked_reason is not None,
            "blocked_reason": self._blocked_reason,
            "reconciliation_failed": self._state_machine.reconciliation_failed,
            "canonical": is_canonical and self._blocked_reason is None,
            "reconciliation_required": self._state_machine.reconciliation_required,
            "queue_pending": self._queue.pending_count(),
            "queue_complete": self._queue.is_complete(),
            "qdrant": {
                "healthy": self.qdrant.is_healthy(),
                "collection": self.config.collection_name,
                "loopback": getattr(self.qdrant, "last_error", None) is None
                or not str(getattr(self.qdrant, "last_error", "")).startswith(
                    "QDRANT_URL_NOT_LOOPBACK"
                ),
                "collection_error": getattr(self.qdrant, "collection_error", None),
            },
            "postgresql": {
                "healthy": self.postgresql.is_healthy(),
            },
            "domain_model": "ok",
            "degraded_pipeline_active": self._degraded_pipeline is not None,
            "reconciler_configured": (
                self._document_fetcher is not None
                and self._embed_texts is not None
            ),
        }
        if self._degraded_pipeline is not None:
            degraded_health = await self._degraded_pipeline.health_check()
            result["degraded_pipeline"] = degraded_health

        # Phase-2 unified status surface
        result["active_generation"] = getattr(self, "_active_generation", None)
        result["canonical_vector_database"] = "qdrant"
        result["embedding_model"] = self.config.embedding_model
        result["embedding_dimension"] = self.config.embedding_dimension
        outbox_stats = getattr(self.postgresql, "outbox_stats", None)
        result["outbox"] = (
            await outbox_stats() if outbox_stats is not None
            else {"pending": self._queue.pending_count()}
        )
        recon_status = getattr(self.postgresql, "reconciliation_status", None)
        result["reconciliation"] = (
            await recon_status() if recon_status is not None else {
                "required": self._state_machine.reconciliation_required,
                "pending": self._queue.pending_count(),
            }
        )
        result["degraded_backend"] = {
            "enabled": True,
            "active": self._degraded_pipeline is not None,
            "canonical": False,
        }
        return result

    # ------------------------------------------------------------------
    # RAG-16D: disaster-recovery rebuild — Qdrant is rebuildable from
    # PostgreSQL authority + qwen3-embedding:4b; SQLite is never the
    # restore source.
    # ------------------------------------------------------------------

    async def rebuild_canonical(
        self,
        generation_manager: Any,
        *,
        module_id: Optional[str] = None,
        batch: int = 50,
        benchmark_fn: Any = None,
    ) -> Any:
        """Rebuild the canonical semantic index from zero.

        Sequence (lifecycle.dr.REBUILD_SEQUENCE):
        START_QDRANT -> CREATE_GENERATION -> READ_PG_METADATA ->
        RESOLVE_SOURCES -> RECHUNK_REEMBED -> VALIDATE -> ACTIVATE.

        Sources come from PostgreSQL (chunk rows carry canonical content);
        resources missing PG chunks are resolved through the owning
        module's ``_document_fetcher`` and re-chunked — never from the
        degraded SQLite store (its 256-dim hashing vectors cannot enter
        the 2560-dim canonical collection).
        """
        from .lifecycle.dr import RebuildStep, evaluate_rebuild

        steps: list[RebuildStep] = []
        rebuilt = 0
        self._migrating = True
        try:
            # START_QDRANT
            if not await self.qdrant.initialize() and not self.qdrant.is_healthy():
                return evaluate_rebuild(tuple(steps), 0, 0)
            steps.append(RebuildStep.START_QDRANT)

            # CREATE_GENERATION (BUILDING — never serves queries)
            generation = await generation_manager.create_generation()
            await generation_manager.ensure_collection(generation)
            steps.append(RebuildStep.CREATE_GENERATION)

            # READ_PG_METADATA — PostgreSQL is the rebuild authority
            list_fn = getattr(self.postgresql, "list_indexed_resources", None)
            resources = (
                await list_fn(module_id) if list_fn is not None else []
            )
            steps.append(RebuildStep.READ_PG_METADATA)

            # RESOLVE_SOURCES + RECHUNK_REEMBED
            for mid, rid in resources:
                chunks = await self.postgresql.fetch_resource_chunks(mid, rid)
                texts = [str(c.get("content") or "") for c in chunks]
                if not any(texts) and self._document_fetcher is not None:
                    doc = await self._call_maybe_async(
                        self._document_fetcher, mid, rid
                    )
                    if doc is None:
                        continue
                    texts = [str(doc.get("content") or "")]
                    chunks = [{
                        "chunk_id": f"{rid}-rebuilt-0",
                        "qdrant_point_id": None,
                        "sequence": 0,
                        "content": texts[0],
                        "payload": {},
                    }]
                if not chunks or not any(texts) or self._embed_texts is None:
                    continue
                vectors = await self._call_maybe_async(self._embed_texts, texts)
                if len(vectors) != len(chunks):
                    continue
                for v in vectors:
                    if len(v) != self.config.embedding_dimension:
                        raise CanonicalCheckError(
                            f"EMBEDDING_DIMENSION_MISMATCH:{len(v)}"
                        )
                from qdrant_client.http.models import PointStruct
                from .rag_qdrant import sanitize_payload
                points = [
                    PointStruct(
                        id=str(
                            c.get("qdrant_point_id")
                            or uuid.uuid5(_POINT_NAMESPACE, c["chunk_id"])
                        ),
                        vector=[float(x) for x in v],
                        payload=sanitize_payload({
                            "module_id": mid,
                            "document_resource_id": rid,
                            "chunk_id": c["chunk_id"],
                            "generation_id": generation.generation_id,
                        }),
                    )
                    for c, v in zip(chunks, vectors)
                ]
                if not await self.qdrant.upsert_points(
                    points, generation_id=generation.generation_id
                ):
                    continue
                rebuilt += 1
            steps.append(RebuildStep.RESOLVE_SOURCES)
            steps.append(RebuildStep.RECHUNK_REEMBED)

            # VALIDATE — dimension/verify + optional benchmark gate
            verification = await generation_manager.verify_generation(generation)
            if not verification.get("ok"):
                return evaluate_rebuild(tuple(steps), rebuilt, len(resources))
            if benchmark_fn is not None:
                bench = await self._call_maybe_async(benchmark_fn, generation)
                if not bench:
                    return evaluate_rebuild(tuple(steps), rebuilt, len(resources))
            steps.append(RebuildStep.VALIDATE)

            # ACTIVATE — atomic alias swap, then bind this pipeline
            if not await generation_manager.promote_to_active(generation):
                return evaluate_rebuild(tuple(steps), rebuilt, len(resources))
            self._active_generation = generation.generation_id
            steps.append(RebuildStep.ACTIVATE)
            return evaluate_rebuild(tuple(steps), rebuilt, len(resources))
        finally:
            self._migrating = False
    # ------------------------------------------------------------------
    # RAG-16: unified status / manifest surface
    # ------------------------------------------------------------------

    def manifest(self) -> Any:
        """RagSystemManifest for this runtime — the machine-readable
        answer to "which architecture produced this answer?"."""
        from .lifecycle.manifest import RagSystemManifest
        from .lifecycle.schema_versions import RagSchemaVersions

        return RagSystemManifest(
            architecture_version="v1",
            policy_version="v1",
            schema=RagSchemaVersions(
                rag_schema_version=1,
                metadata_schema_version=1,
                vector_schema_version=1,
            ),
            active_generation=self._active_generation or "",
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            collection_alias=self.config.collection_name,
            canonical_state=self.gateway_state(),
            degraded_backend="sqlite-bounded-fallback",
        )

    async def status(self) -> dict[str, Any]:
        """Unified status surface: manifest core + health + metrics."""
        health = await self.health_check()
        metrics = getattr(self, "_metrics", None)
        from .observability import RAG_METRICS
        metrics = metrics or RAG_METRICS
        # Feed gauges from the live stores.
        outbox = health.get("outbox") or {}
        metrics.set_gauge("outbox_pending", int(outbox.get("pending") or 0))
        metrics.set_gauge("outbox_retry", int(outbox.get("retry") or 0))
        metrics.set_gauge(
            "outbox_dead_letter", int(outbox.get("dead_letter") or 0)
        )
        recon = health.get("reconciliation") or {}
        metrics.set_gauge(
            "reconciliation_pending",
            int(recon.get("pending_count") or recon.get("pending") or 0),
        )
        metrics.set_gauge(
            "reconciliation_failed", int(recon.get("failed_count") or 0)
        )
        metrics.set_gauge("active_generation", self._active_generation or "")
        return {
            "state": self.gateway_state(),
            "canonical": self.gateway_state() == "CANONICAL_READY",
            "blocked_reason": self._blocked_reason,
            "manifest": {
                "architecture_version": self.manifest().architecture_version,
                "active_generation": self._active_generation or "",
                "embedding_model": self.config.embedding_model,
                "embedding_dimension": self.config.embedding_dimension,
                "collection_alias": self.config.collection_name,
                "sub_architectures": list(self.manifest().sub_architectures),
            },
            "health": health,
            "metrics": metrics.snapshot(),
        }

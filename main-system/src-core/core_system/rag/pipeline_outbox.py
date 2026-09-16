"""RAG-08/09/10 — canonical outbox driver + delete guarantee.

``gptbridge_rag.outbox_event`` is THE canonical outbox: resource/chunk/
index_state metadata and the outbox event commit in ONE PostgreSQL
transaction; Qdrant writes are NEVER part of it — they are applied by
this driver (synchronously on the request path, or replayed by
``process_outbox`` after a crash).

Guarantees:
- Payloads carry opaque references only (chunk_ids/point_ids) — never
  content, never physical locators.
- Every operation is idempotent: deterministic point ids make a replayed
  UPSERT an overwrite; DELETE verifies zero remaining points.
- DEAD_LETTER is observable through ``outbox_stats()`` — never dropped.
- Tombstone-first delete: PostgreSQL tombstone makes the resource
  logically invisible BEFORE any physical delete, and a failed Qdrant
  delete can never make it visible again.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from qdrant_client.http.models import PointStruct

from .rag_contracts import OutboxOperation, OutboxState
from .rag_qdrant import sanitize_payload

_logger = logging.getLogger("gptbridge.rag")

_OUTBOX_MAX_ATTEMPTS = 5
_OUTBOX_RETRY_BASE_SECONDS = 2.0

# A record tombstone/provenance update cannot physically delete; purge is
# a separate, explicit operation.
_PURGE_REASON = "purged"


class PipelineOutboxMixin:
    """Canonical outbox + tombstone/delete orchestration (RAG-08/10)."""

    # -- event construction ---------------------------------------------------

    @staticmethod
    def _new_outbox_event(
        *,
        operation: OutboxOperation,
        module_id: str,
        resource_id: str,
        source_version: int = 0,
        content_hash: str = "",
        generation_id: str = "",
        request_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "event_id": str(uuid.uuid4()),
            "request_id": request_id or str(uuid.uuid4()),
            "operation": operation.value,
            "module_id": module_id,
            "resource_id": resource_id,
            "source_version": int(source_version),
            "content_hash": content_hash,
            "generation_id": generation_id,
            "payload": payload or {},
        }

    # -- event application -----------------------------------------------------

    async def _apply_outbox_event(self, event: dict[str, Any]) -> bool:
        """Apply one outbox event to Qdrant + index_state writeback.

        All branches are idempotent: replays converge to the same state.
        """
        op = str(event.get("operation") or "")
        module_id = str(event.get("module_id") or "")
        resource_id = str(event.get("resource_id") or "")
        generation_id = str(event.get("generation_id") or "") or None
        payload = event.get("payload") or {}

        if op in (
            OutboxOperation.DELETE_RESOURCE.value,
            OutboxOperation.DELETE.value,
        ):
            # delete_resource verifies zero remaining points itself
            return await self.qdrant.delete_resource(
                module_id=module_id,
                resource_id=resource_id,
                generation_id=generation_id,
            )

        if op not in (
            OutboxOperation.UPSERT_RESOURCE.value,
            OutboxOperation.REINDEX_RESOURCE.value,
            OutboxOperation.RECONCILE_RESOURCE.value,
            OutboxOperation.UPSERT.value,
            OutboxOperation.REINDEX.value,
            OutboxOperation.UPDATE_METADATA.value,
        ):
            _logger.warning("PipelineOutbox: unknown operation %s", op)
            return False

        # UPSERT family: rebuild points from PostgreSQL truth (never from
        # event payload vectors — payloads carry references only).
        chunks = await self.postgresql.fetch_resource_chunks(
            module_id, resource_id
        )
        wanted_ids = [str(i) for i in payload.get("chunk_ids") or ()]
        if wanted_ids:
            chunks = [c for c in chunks if c["chunk_id"] in wanted_ids]
        if not chunks:
            _logger.warning(
                "PipelineOutbox: no canonical chunks for %s:%s",
                module_id, resource_id,
            )
            return False

        if self._embed_texts is None:
            raise RuntimeError(
                "outbox replay requires embed_texts — vectors are never "
                "stored in the outbox payload"
            )
        texts = [str(c.get("content") or "") for c in chunks]
        vectors = await self._call_maybe_async(self._embed_texts, texts)
        if len(vectors) != len(texts):
            return False
        for vector in vectors:
            if len(vector) != self.config.embedding_dimension:
                raise RuntimeError(
                    f"EMBEDDING_DIMENSION_MISMATCH: {len(vector)}-dim cannot "
                    f"enter {self.config.embedding_dimension}-dim collection"
                )

        points = [
            PointStruct(
                id=str(c.get("qdrant_point_id") or c.get("point_id")),
                vector=[float(v) for v in vector],
                payload=sanitize_payload(
                    {
                        "module_id": module_id,
                        "document_resource_id": resource_id,
                        "chunk_id": c["chunk_id"],
                        "generation_id": generation_id or "",
                        "content_hash": c.get("payload", {}).get(
                            "content_hash", ""
                        ),
                    }
                ),
            )
            for c, vector in zip(chunks, vectors)
        ]
        if not await self.qdrant.upsert_points(
            points, generation_id=generation_id
        ):
            return False
        # index_state writeback only after Qdrant confirms
        await self.postgresql.upsert_index_state(
            self._outbox_index_state(event, chunks),
        )
        return True

    def _outbox_index_state(self, event: dict[str, Any], chunks: list) -> Any:
        from .rag_qdrant import IndexState

        first = chunks[0] if chunks else {}
        return IndexState(
            resource_id=str(event["resource_id"]),
            module_id=str(event["module_id"]),
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            chunk_size=int(self.config.chunk_size),
            chunk_overlap=int(self.config.chunk_overlap),
            indexed_at_utc=datetime.now(timezone.utc).isoformat(),
            content_hash=str(event.get("content_hash") or ""),
            qdrant_point_id=str(
                first.get("qdrant_point_id") or first.get("point_id") or ""
            ),
            postgresql_record_id=str(event["resource_id"]),
        )

    # -- replay driver ----------------------------------------------------------

    async def process_outbox(self, batch: int = 25) -> dict[str, int]:
        """Drain pending/due outbox events — idempotent, restart-safe.

        Called inline on the write path after the canonical transaction
        commits, and by recovery after a crash.  Bounded batch; retry with
        exponential backoff; DEAD_LETTER is observable via outbox_stats.
        """
        stats = {"processed": 0, "succeeded": 0, "retried": 0,
                 "dead_lettered": 0}
        fetch = getattr(self.postgresql, "fetch_outbox_events", None)
        if fetch is None:
            return stats
        events = await fetch(batch)
        for event in events:
            stats["processed"] += 1
            event_id = str(event["event_id"])
            attempts = int(event.get("attempt_count") or 0) + 1
            try:
                ok = await self._apply_outbox_event(event)
            except Exception as exc:
                ok = False
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                last_error = "apply returned False"
            if ok:
                await self.postgresql.mark_outbox(
                    event_id, OutboxState.SUCCEEDED.value, terminal=True
                )
                stats["succeeded"] += 1
                continue
            if attempts >= _OUTBOX_MAX_ATTEMPTS:
                await self.postgresql.mark_outbox(
                    event_id, OutboxState.DEAD_LETTER.value,
                    error=last_error, terminal=True,
                )
                stats["dead_lettered"] += 1
                _logger.error(
                    "PipelineOutbox: %s DEAD_LETTER after %d attempts: %s",
                    event_id, attempts, last_error,
                )
            else:
                delay = _OUTBOX_RETRY_BASE_SECONDS ** attempts
                retry_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat()
                await self.postgresql.mark_outbox(
                    event_id, OutboxState.RETRY.value,
                    error=last_error, next_retry_at=retry_at,
                )
                stats["retried"] += 1
        return stats

    # -- RAG-10: tombstone / delete guarantee -----------------------------------

    async def delete_resource(
        self,
        *,
        module_id: str,
        resource_id: str,
        source_version: int = 0,
        content_hash: str = "",
        generation_id: str = "",
        request_id: Optional[str] = None,
        purge: bool = False,
    ) -> bool:
        """Tombstone-first delete guarantee.

        Order: PG tombstone (logical invisibility, immediate) → canonical
        outbox DELETE_RESOURCE → Qdrant delete (verified) → degraded SQLite
        delete → provenance derived marked stale → index_state='deleted'.

        A failed Qdrant delete leaves the event PENDING for replay and can
        never resurrect the resource — the read barrier reads the
        tombstone, not the vector index.  ``purge=True`` is a separate,
        explicit physical purge — never implied by delete.
        """
        module_id = str(module_id)
        resource_id = str(resource_id)
        if not module_id or not resource_id:
            return False

        # 1. Authoritative tombstone first — read barrier applies instantly.
        await self.postgresql.raise_tombstone(
            module_id=module_id,
            resource_id=resource_id,
            source_revision=source_version,
            content_hash=content_hash,
            reason=_PURGE_REASON if purge else "deleted",
        )

        # 2. Derived/provenance resources must not stay canonical evidence.
        mark_stale = getattr(self.postgresql, "mark_derived_stale", None)
        if mark_stale is not None:
            await mark_stale(module_id, resource_id)

        # 3. Durable outbox event so the physical delete survives crashes.
        event = self._new_outbox_event(
            operation=OutboxOperation.DELETE_RESOURCE,
            module_id=module_id,
            resource_id=resource_id,
            source_version=source_version,
            content_hash=content_hash,
            generation_id=generation_id,
            request_id=request_id,
        )
        insert = getattr(self.postgresql, "insert_outbox_event", None)
        if insert is not None:
            await insert(event)

        # 4. Physical delete attempt — failure is fine: tombstone holds.
        applied = await self._apply_outbox_event(event)
        if applied:
            mark = getattr(self.postgresql, "mark_index_state", None)
            if mark is not None:
                await mark(module_id, resource_id, "deleted")
            await self.postgresql.mark_outbox(
                event["event_id"], OutboxState.SUCCEEDED.value,
                terminal=True,
            )
        else:
            # Event remains PENDING — replayed by process_outbox once
            # Qdrant is reachable; the tombstone keeps reads closed.
            await self.postgresql.mark_outbox(
                event["event_id"], OutboxState.RETRY.value,
                error="qdrant delete pending",
                next_retry_at=(
                    datetime.now(timezone.utc)
                    + timedelta(seconds=_OUTBOX_RETRY_BASE_SECONDS)
                ).isoformat(),
            )

        # 5. Degraded SQLite mirror delete (bounded fallback store).
        if self._degraded_pipeline is not None:
            try:
                store = getattr(self._degraded_pipeline, "vector_store", None)
                deleter = getattr(store, "delete", None) or getattr(
                    store, "delete_resource", None
                )
                if deleter is not None:
                    deleter(resource_id=resource_id, module_id=module_id)
            except Exception as exc:
                _logger.warning(
                    "PipelineOutbox: degraded delete failed for %s:%s: %s",
                    module_id, resource_id, exc,
                )
        return True

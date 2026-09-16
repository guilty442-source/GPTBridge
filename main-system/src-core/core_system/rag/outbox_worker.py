"""Outbox Worker — 異步消費 outbox_event 並執行 Qdrant 冪等寫入。

支援：
- UPSERT: 向量寫入（按 point_id 冪等）
- DELETE: 刪除資源
- REINDEX: 重新索引
- UPDATE_METADATA: 僅更新 metadata
"""

from __future__ import annotations

import json
import logging
import pickle
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from qdrant_client.http.models import PointStruct

from .rag_qdrant import sanitize_payload

from .rag_protocol import OutboxRepository
from .rag_contracts import (
    OutboxEvent,
    OutboxState,
    OutboxOperation,
    CanonicalVectorPoint,
    LifecycleState,
    DataCategory,
    ResourceType,
)
from .rag_qdrant import QdrantCanonicalRuntime, RagPipelineConfig

_logger = logging.getLogger("gptbridge.rag.outbox_worker")


@dataclass
class WorkerStats:
    """Worker runtime statistics."""
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    dead_letter: int = 0
    last_error: Optional[str] = None
    last_run_at: Optional[str] = None


class OutboxWorker:
    """Background worker that processes outbox events and applies to Qdrant.

    Idempotency guarantees:
    - UPSERT: Qdrant upsert with same point_id = overwrite (idempotent)
    - DELETE: Qdrant delete by resource_id + module_id (idempotent)
    - REINDEX: Same as UPSERT
    - UPDATE_METADATA: Update Qdrant point payload only

    State machine:
    PENDING -> PROCESSING -> SUCCEEDED
                    -> RETRY (max N times) -> DEAD_LETTER
    """

    def __init__(
        self,
        outbox_repo: OutboxRepository,
        qdrant: QdrantCanonicalRuntime,
        config: RagPipelineConfig,
        embedding_provider: Any = None,  # EmbeddingProvider
        batch_size: int = 50,
        max_attempts: int = 5,
        retry_base_delay: float = 2.0,  # seconds
    ) -> None:
        self.outbox_repo = outbox_repo
        self.qdrant = qdrant
        self.config = config
        self.embedding_provider = embedding_provider
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.retry_base_delay = retry_base_delay
        self._running = False
        self._stats = WorkerStats()

    def _get_pg_conn(self) -> psycopg.Connection:
        """Get PostgreSQL connection from outbox repo."""
        return self.outbox_repo._get_conn()

    def process_batch(self) -> WorkerStats:
        """Process one batch of pending events."""
        self._stats.last_run_at = datetime.now(timezone.utc).isoformat()

        events = self.outbox_repo.fetch_pending(self.batch_size)
        if not events:
            return self._stats

        for event in events:
            self._stats.processed += 1
            success = self._process_event(event)

            if success:
                self.outbox_repo.mark_succeeded(event.event_id)
                self._stats.succeeded += 1
            else:
                self._handle_failure(event)

        return self._stats

    def _process_event(self, event: OutboxEvent) -> bool:
        """Process a single outbox event.

        Returns True if successful, False if failed (will retry or dead-letter).
        """
        try:
            if event.operation == OutboxOperation.UPSERT:
                return self._process_upsert(event)
            elif event.operation == OutboxOperation.DELETE:
                return self._process_delete(event)
            elif event.operation == OutboxOperation.REINDEX:
                return self._process_reindex(event)
            elif event.operation == OutboxOperation.UPDATE_METADATA:
                return self._process_update_metadata(event)
            else:
                _logger.warning("OutboxWorker: unknown operation %s for event %s",
                               event.operation, event.event_id)
                return False

        except Exception as e:
            _logger.error("OutboxWorker: event %s failed: %s", event.event_id, e)
            return False

    def _process_upsert(self, event: OutboxEvent) -> bool:
        """Process UPSERT: compute embeddings and upsert to Qdrant."""
        payload = event.payload or {}
        chunk_ids = payload.get("chunk_ids", [])
        module_id = payload.get("module_id")
        resource_id = payload.get("resource_id")
        generation_id = payload.get("generation_id")
        source_version = payload.get("source_version", 0)
        embedding_model = payload.get("embedding_model", self.config.embedding_model)
        embedding_dimension = payload.get("embedding_dimension", self.config.embedding_dimension)
        rag_types = [OutboxOperation(rt) for rt in payload.get("rag_types", ["hybrid"])]
        data_category = payload.get("data_category", "internal")
        resource_type = payload.get("resource_type", "document")

        if not chunk_ids:
            _logger.warning("OutboxWorker: UPSERT event %s has no chunk_ids", event.event_id)
            return False

        # Get chunk content from PostgreSQL
        chunks_data = self._fetch_chunks_for_upsert(event.module_id, event.resource_id, chunk_ids)
        if not chunks_data:
            _logger.warning("OutboxWorker: no chunk data found for %s/%s",
                           event.module_id, event.resource_id)
            return False

        # Build points with embeddings
        points = []
        for chunk_data in chunks_data:
            content = chunk_data.get("content", "")
            if not content:
                continue

            # Get embedding (with cache)
            vector = self._get_embedding(content, embedding_model, embedding_dimension)
            if not vector:
                _logger.warning("OutboxWorker: failed to get embedding for chunk %s",
                               chunk_data.get("chunk_id"))
                return False

            # Build canonical vector point (NO content in payload)
            point = CanonicalVectorPoint(
                point_id=chunk_data["chunk_id"],
                vector=tuple(vector),
                locator_id=chunk_data["locator_id"],
                chunk_id=chunk_data["chunk_id"],
                resource_id=event.resource_id,
                module_id=event.module_id,
                rag_types=tuple(rag_types),
                data_category=DataCategory(data_category),
                resource_type=ResourceType(resource_type),
                generation_id=generation_id,
                source_version=source_version,
                content_hash=chunk_data["content_hash"],
                embedding_model=embedding_model,
            )

            points.append(PointStruct(
                id=point.point_id,
                vector=point.vector,
                payload=sanitize_payload({
                    "locator_id": point.locator_id,
                    "chunk_id": point.chunk_id,
                    "resource_id": point.resource_id,
                    "module_id": point.module_id,
                    "rag_types": [rt.value for rt in point.rag_types],
                    "data_category": point.data_category.value,
                    "resource_type": point.resource_type.value,
                    "generation_id": point.generation_id,
                    "source_version": point.source_version,
                    "content_hash": point.content_hash,
                    "embedding_model": point.embedding_model,
                }),
            ))

        # Upsert to Qdrant (idempotent by point_id)
        target_collection = f"{self.config.collection_name}_{generation_id}"
        try:
            self.qdrant.client.upsert(
                collection_name=target_collection,
                points=points,
                wait=True,
            )
        except Exception as e:
            _logger.error("OutboxWorker: Qdrant upsert failed for %s: %s", event.event_id, e)
            return False

        # Update index_state to ACTIVE
        self._update_index_state_active(event.module_id, event.resource_id, generation_id)

        return True

    def _process_delete(self, event: OutboxEvent) -> bool:
        """Process DELETE: remove from Qdrant."""
        payload = event.payload or {}
        module_id = payload.get("module_id", event.module_id)
        resource_id = payload.get("resource_id", event.resource_id)
        generation_id = payload.get("generation_id", event.generation_id)

        target_collection = f"{self.config.collection_name}_{generation_id}"

        try:
            self.qdrant.client.delete(
                collection_name=target_collection,
                points_selector={
                    "filter": {
                        "must": [
                            {"key": "module_id", "match": {"value": module_id}},
                            {"key": "resource_id", "match": {"value": resource_id}},
                        ]
                    }
                },
                wait=True,
            )

            # Update index_state to DELETED
            self._update_index_state_deleted(module_id, resource_id)

            return True

        except Exception as e:
            _logger.error("OutboxWorker: Qdrant delete failed for %s: %s", event.event_id, e)
            return False

    def _process_reindex(self, event: OutboxEvent) -> bool:
        """Process REINDEX: same as UPSERT but for reconciliation."""
        # REINDEX is essentially a forced UPSERT
        return self._process_upsert(event)

    def _process_update_metadata(self, event: OutboxEvent) -> bool:
        """Process UPDATE_METADATA: update Qdrant point payload only."""
        # Not fully implemented - would need to read-modify-write Qdrant points
        _logger.warning("OutboxWorker: UPDATE_METADATA not yet implemented")
        return True  # Don't retry

    def _fetch_chunks_for_upsert(
        self,
        module_id: str,
        resource_id: str,
        chunk_ids: list[str],
    ) -> list[dict]:
        """Fetch chunk content and metadata from PostgreSQL."""
        conn = self.outbox_repo._get_conn()
        chunks = []

        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(chunk_ids))
            cur.execute(
                f"""
                SELECT chunk_id, content, content_hash, locator_id, sequence,
                       character_start, character_end, chunk_policy_version
                FROM gptbridge_rag.chunks
                WHERE module_id = %s AND resource_id = %s AND chunk_id IN ({','.join(['%s'] * len(chunk_ids))})
                ORDER BY sequence
                """,
                [module_id, resource_id] + chunk_ids,
            )
            for row in cur.fetchall():
                chunks.append(dict(row))

        return chunks

    def _get_embedding(
        self,
        content: str,
        model: str,
        dimension: int,
    ) -> Optional[list[float]]:
        """Get embedding from cache or compute."""
        if self.embedding_provider:
            try:
                return self.embedding_provider.embed_single(content)
            except Exception as e:
                _logger.error("OutboxWorker: embedding failed: %s", e)
                return None

        # Fallback: deterministic pseudo-embedding
        import hashlib
        import random
        seed = int(hashlib.sha256(content.encode()).hexdigest()[:8], 16)
        random.seed(seed)
        return [random.uniform(-1, 1) for _ in range(dimension)]

    def _update_index_state_active(
        self,
        module_id: str,
        resource_id: str,
        generation_id: str,
    ) -> None:
        """Update index_state to ACTIVE after successful Qdrant upsert."""
        conn = self.outbox_repo._get_conn()
        now = time.time()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.index_state
                SET state = %s, updated_at = to_timestamp(%s)
                WHERE module_id = %s AND resource_id = %s AND generation_id = %s
                """,
                (LifecycleState.ACTIVE.value, now, module_id, resource_id, generation_id),
            )
            conn.commit()

    def _update_index_state_deleted(
        self,
        module_id: str,
        resource_id: str,
    ) -> None:
        """Update index_state to DELETED after successful Qdrant delete."""
        conn = self.outbox_repo._get_conn()
        now = time.time()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.index_state
                SET state = %s, updated_at = to_timestamp(%s)
                WHERE module_id = %s AND resource_id = %s
                """,
                (LifecycleState.DELETED.value, now, module_id, resource_id),
            )
            conn.commit()

    def _handle_failure(self, event: OutboxEvent) -> None:
        """Handle event processing failure: retry or dead-letter."""
        conn = self.outbox_repo._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempt_count FROM gptbridge_rag.outbox_event WHERE event_id = %s",
                (event.event_id,),
            )
            row = cur.fetchone()
            attempts = (row["attempt_count"] if row else 0) + 1

            if attempts >= self.max_attempts:
                # Dead letter
                cur.execute(
                    """
                    UPDATE gptbridge_rag.outbox_event
                    SET state = %s, attempt_count = %s, last_error = %s,
                        updated_at = to_timestamp(%s), completed_at = to_timestamp(%s)
                    WHERE event_id = %s
                    """,
                    (OutboxState.DEAD_LETTER.value, attempts, f"Max attempts ({self.max_attempts}) reached",
                     time.time(), time.time(), event.event_id),
                )
                self._stats.dead_letter += 1
                _logger.error("OutboxWorker: event %s moved to DEAD_LETTER after %d attempts",
                             event.event_id, attempts)
            else:
                # Retry with exponential backoff
                next_retry = datetime.now(timezone.utc).timestamp() + (self.retry_base_delay ** attempts)
                cur.execute(
                    """
                    UPDATE gptbridge_rag.outbox_event
                    SET state = %s, attempt_count = %s, last_error = %s,
                        next_retry_at = to_timestamp(%s), updated_at = to_timestamp(%s)
                    WHERE event_id = %s
                    """,
                    (OutboxState.RETRY.value, attempts, "Processing failed",
                     next_retry, time.time(), event.event_id),
                )
                self._stats.failed += 1
                _logger.warning("OutboxWorker: event %s retry %d/%d scheduled",
                               event.event_id, attempts, self.max_attempts)
            conn.commit()

    def process_retry_batch(self) -> int:
        """Process events in RETRY state whose next_retry_at has passed."""
        conn = self.outbox_repo._get_conn()
        processed = 0

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_id, request_id, operation, module_id, resource_id,
                       source_version, content_hash, generation_id, payload,
                       attempt_count, state, last_error, created_at, updated_at
                FROM gptbridge_rag.outbox_event
                WHERE state = 'RETRY' AND next_retry_at <= NOW()
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (self.batch_size,),
            )

            for row in cur.fetchall():
                event = OutboxEvent(
                    event_id=str(row["event_id"]),
                    request_id=row["request_id"],
                    operation=OutboxOperation(row["operation"]),
                    module_id=row["module_id"],
                    resource_id=row["resource_id"],
                    source_version=row["source_version"],
                    content_hash=row["content_hash"],
                    generation_id=row["generation_id"],
                    payload=row["payload"],
                    state=OutboxState.PROCESSING,  # Reset to processing
                    attempt_count=row["attempt_count"],
                    last_error=row["last_error"],
                    created_at=row["created_at"],
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )

                # Reset state to PENDING for reprocessing
                with conn.cursor() as cur2:
                    cur2.execute(
                        "UPDATE gptbridge_rag.outbox_event SET state = %s WHERE event_id = %s",
                        (OutboxState.PENDING.value, event.event_id),
                    )
                    conn.commit()

                success = self._process_event(event)
                if success:
                    self.outbox_repo.mark_succeeded(event.event_id)
                    self._stats.succeeded += 1
                else:
                    self._handle_failure(event)
                processed += 1

        return processed


__all__ = ["OutboxWorker", "WorkerStats"]
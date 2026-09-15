"""RAG canonical reconciliation, tombstone and saga stores (A374).

Mixed into ``PostgreSQLMetadataAuthority``: durable reconciliation queue,
cross-store outbox steps, tombstone anti-resurrection and the extended
index_state upsert all share the authority's managed async connection.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .rag_qdrant import IndexState
from .runtime_state import ReconciliationQueueItem

_logger = logging.getLogger("gptbridge.rag")


_TOMBSTONE_SELECT_SQL = """SELECT tombstone_generation, source_revision
   FROM gptbridge_rag.tombstone
   WHERE module_id = %s AND resource_id = %s"""

_TOMBSTONE_UPSERT_SQL = """INSERT INTO gptbridge_rag.tombstone
      (resource_id, module_id, tombstone_generation,
       source_revision, content_hash, reason)
   VALUES (%s, %s, %s, %s, %s, %s)
   ON CONFLICT (module_id, resource_id) DO UPDATE SET
       tombstone_generation = EXCLUDED.tombstone_generation,
       source_revision = EXCLUDED.source_revision,
       content_hash = EXCLUDED.content_hash,
       reason = EXCLUDED.reason,
       created_at = now(),
       purged = false"""

_INDEX_STATE_EXTENDED_UPSERT_SQL = """INSERT INTO gptbridge_rag.index_state
      (resource_id, module_id, embedding_model, embedding_dimension,
       chunk_size, chunk_overlap, indexed_at, content_hash,
       qdrant_point_id, postgresql_record_id, source_revision,
       tombstone_generation, embedding_version, chunking_version,
       parser_version, rag_schema_version, pipeline_version,
       backend_generation, status)
   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
   ON CONFLICT (resource_id) DO UPDATE SET
       module_id = EXCLUDED.module_id,
       embedding_model = EXCLUDED.embedding_model,
       embedding_dimension = EXCLUDED.embedding_dimension,
       chunk_size = EXCLUDED.chunk_size,
       chunk_overlap = EXCLUDED.chunk_overlap,
       indexed_at = EXCLUDED.indexed_at,
       content_hash = EXCLUDED.content_hash,
       qdrant_point_id = EXCLUDED.qdrant_point_id,
       postgresql_record_id = EXCLUDED.postgresql_record_id,
       source_revision = EXCLUDED.source_revision,
       tombstone_generation = EXCLUDED.tombstone_generation,
       embedding_version = EXCLUDED.embedding_version,
       chunking_version = EXCLUDED.chunking_version,
       parser_version = EXCLUDED.parser_version,
       rag_schema_version = EXCLUDED.rag_schema_version,
       pipeline_version = EXCLUDED.pipeline_version,
       backend_generation = EXCLUDED.backend_generation,
       status = EXCLUDED.status"""


def _extended_index_state_params(
    state: IndexState,
    source_revision: int,
    tombstone_generation: int,
    embedding_version: int,
    chunking_version: int,
    parser_version: int,
    rag_schema_version: int,
    pipeline_version: int,
    backend_generation: int,
    status: str,
) -> tuple:
    """INSERT params for the extended index_state upsert."""
    return (
        state.resource_id, state.module_id, state.embedding_model,
        state.embedding_dimension, state.chunk_size, state.chunk_overlap,
        state.indexed_at_utc, state.content_hash, state.qdrant_point_id,
        state.postgresql_record_id, source_revision, tombstone_generation,
        embedding_version, chunking_version, parser_version,
        rag_schema_version, pipeline_version, backend_generation, status,
    )


class RagMetadataReconciliationMixin:
    """A374 durable stores: tombstone, reconciliation queue, outbox steps."""

    # -- A374: Tombstone anti-resurrection (authoritative PostgreSQL store) --

    async def raise_tombstone(
        self,
        *,
        module_id: str,
        resource_id: str,
        source_revision: int,
        content_hash: str,
        reason: str = "deleted",
    ) -> Optional[dict[str, Any]]:
        """A374: write authoritative tombstone first, monotonic generation."""
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(_TOMBSTONE_SELECT_SQL, (module_id, resource_id))
                row = await cur.fetchone()
                existing_gen = int(row[0]) if row else 0
                existing_rev = int(row[1]) if row else 0
                if row and existing_rev >= source_revision:
                    _logger.warning(
                        "PostgreSQLMetadataAuthority: stale tombstone rejected "
                        "existing=%s requested=%s", existing_rev, source_revision
                    )
                    return None
                next_gen = existing_gen + 1
                await cur.execute(
                    _TOMBSTONE_UPSERT_SQL,
                    (resource_id, module_id, next_gen, source_revision, content_hash, reason),
                )
                return {
                    "resource_id": resource_id,
                    "module_id": module_id,
                    "tombstone_generation": next_gen,
                    "source_revision": source_revision,
                    "content_hash": content_hash,
                    "reason": reason,
                }
        except Exception as exc:
            _logger.error("PostgreSQLMetadataAuthority: raise_tombstone failed: %s", exc)
            return None

    async def get_tombstone(
        self, module_id: str, resource_id: str
    ) -> Optional[dict[str, Any]]:
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT resource_id, module_id, tombstone_generation,
                          source_revision, content_hash, reason, purged
                       FROM gptbridge_rag.tombstone
                       WHERE module_id = %s AND resource_id = %s""",
                    (module_id, resource_id),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                return {
                    "resource_id": row[0],
                    "module_id": row[1],
                    "tombstone_generation": int(row[2]),
                    "source_revision": int(row[3]),
                    "content_hash": row[4],
                    "reason": row[5],
                    "purged": bool(row[6]),
                }
        except Exception as exc:
            _logger.error("PostgreSQLMetadataAuthority: get_tombstone failed: %s", exc)
            return None

    async def is_tombstoned(self, module_id: str, resource_id: str) -> bool:
        record = await self.get_tombstone(module_id, resource_id)
        return record is not None and not record.get("purged", False)

    # -- A374: Durable reconciliation queue (canonical PostgreSQL store) ------

    async def enqueue_reconciliation(
        self,
        item: "ReconciliationQueueItem",
    ) -> bool:
        """A374: durable enqueue on the canonical PostgreSQL queue."""
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO gptbridge_rag.reconciliation_queue
                          (operation_id, idempotency_key, resource_id, locator_id,
                           source_revision, content_hash, operation, tombstone_generation,
                           embedding_model, embedding_version, chunk_size, chunk_overlap,
                           chunking_version, parser_version, schema_version, created_at,
                           attempts, next_retry_at, deadline, status, correlation_id,
                           last_error, degraded_indexed_at, payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (idempotency_key) DO UPDATE SET
                           status = 'pending', last_error = NULL,
                           next_retry_at = NULL, canonical_synced_at = NULL""",
                    (
                        item.operation_id, item.idempotency_key, item.resource_id,
                        item.locator_id, item.source_revision, item.content_hash,
                        item.operation, item.tombstone_generation,
                        item.embedding_model, item.embedding_version,
                        item.chunk_size, item.chunk_overlap,
                        item.chunking_version, item.parser_version, item.schema_version,
                        item.created_at, item.attempts, item.next_retry_at, item.deadline,
                        item.status, item.correlation_id, item.last_error,
                        item.degraded_indexed_at or item.created_at,
                        json.dumps(item.payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
            return True
        except Exception as exc:
            _logger.error("PostgreSQLMetadataAuthority: enqueue_reconciliation failed: %s", exc)
            return False

    async def pending_reconciliation_count(self) -> int:
        if not self._healthy or not self._conn:
            return 0
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT COUNT(*) FROM gptbridge_rag.reconciliation_queue
                       WHERE status IN ('pending','leased','reconciling','failed')"""
                )
                row = await cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:
            _logger.error("PostgreSQLMetadataAuthority: pending_reconciliation_count failed: %s", exc)
            return 0

    async def queue_is_complete(self) -> bool:
        return (await self.pending_reconciliation_count()) == 0

    async def mark_reconciled(self, operation_id: str) -> bool:
        """Stamp canonical_synced_at + verified on the canonical queue row."""
        return await self._recon_status(
            operation_id,
            "status='verified', canonical_synced_at=now(), "
            "last_error=NULL, next_retry_at=NULL",
            (),
        )

    async def mark_recon_retry(
        self, operation_id: str, error: str, retry_at: Optional[str]
    ) -> bool:
        return await self._recon_status(
            operation_id,
            "status='pending', last_error=%s, next_retry_at=%s",
            (error[:400], retry_at),
        )

    async def dead_letter_reconciliation(
        self, operation_id: str, reason: str
    ) -> bool:
        return await self._recon_status(
            operation_id,
            "status='dead_letter', last_error=%s, next_retry_at=NULL",
            (reason[:400],),
        )

    async def _recon_status(
        self, operation_id: str, set_clause: str, params: tuple
    ) -> bool:
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    f"""UPDATE gptbridge_rag.reconciliation_queue
                        SET {set_clause} WHERE operation_id=%s""",
                    (*params, operation_id),
                )
            return True
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: reconciliation status update failed: %s",
                exc,
            )
            return False

    async def drain_reconciled(self) -> int:
        if not self._healthy or not self._conn:
            return 0
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """DELETE FROM gptbridge_rag.reconciliation_queue
                       WHERE status='verified'"""
                )
                return int(cur.rowcount or 0)
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: drain_reconciled failed: %s", exc
            )
            return 0

    async def index_state_summary(
        self, embedding_model: str, embedding_dimension: int
    ) -> Optional[dict[str, int]]:
        """Aggregate canonical index_state coverage for reconciliation parity."""
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT COUNT(*),
                              COALESCE(SUM(chunk_count), 0),
                              SUM(CASE WHEN qdrant_point_id IS NULL THEN 1 ELSE 0 END),
                              SUM(CASE WHEN content_hash IS NULL OR content_hash = '' THEN 1 ELSE 0 END),
                              SUM(CASE WHEN embedding_model IS DISTINCT FROM %s
                                        OR embedding_dimension IS DISTINCT FROM %s
                                       THEN 1 ELSE 0 END)
                       FROM gptbridge_rag.index_state""",
                    (embedding_model, embedding_dimension),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                return {
                    "documents": int(row[0] or 0),
                    "total_chunks": int(row[1] or 0),
                    "missing_point_ids": int(row[2] or 0),
                    "missing_hashes": int(row[3] or 0),
                    "mismatched_versions": int(row[4] or 0),
                }
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: index_state_summary failed: %s", exc
            )
            return None

    async def record_outbox_step(
        self,
        *,
        step_id: str,
        operation_id: str,
        store: str,
        operation: str,
        succeeded: bool,
        error: Optional[str] = None,
        store_record_id: Optional[str] = None,
    ) -> bool:
        """A374: record a cross-store outbox step (saga audit trail)."""
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO gptbridge_rag.outbox_step
                          (step_id, operation_id, store, operation, succeeded,
                           error, store_record_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (step_id, operation_id, store, operation, succeeded, error, store_record_id),
                )
            return True
        except Exception as exc:
            _logger.error("PostgreSQLMetadataAuthority: record_outbox_step failed: %s", exc)
            return False

    # -- A374: extended index_state upsert (revision + tombstone) ------------

    async def upsert_index_state_extended(
        self,
        state: IndexState,
        *,
        source_revision: int = 1,
        tombstone_generation: int = 0,
        embedding_version: int = 1,
        chunking_version: int = 1,
        parser_version: int = 1,
        rag_schema_version: int = 1,
        pipeline_version: int = 1,
        backend_generation: int = 1,
        status: str = "indexed",
    ) -> bool:
        """A374: upsert index_state with authoritative revision/tombstone."""
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    _INDEX_STATE_EXTENDED_UPSERT_SQL,
                    _extended_index_state_params(
                        state, source_revision, tombstone_generation,
                        embedding_version, chunking_version, parser_version,
                        rag_schema_version, pipeline_version, backend_generation, status,
                    ),
                )
            return True
        except Exception as exc:
            _logger.error("PostgreSQLMetadataAuthority: upsert_index_state_extended failed: %s", exc)
            return False

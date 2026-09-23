"""RAG canonical reconciliation, tombstone and saga stores (A374).

Mixed into ``PostgreSQLMetadataAuthority``: durable reconciliation queue,
cross-store outbox steps, tombstone anti-resurrection and the extended
index_state upsert all share the authority's managed async connection.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

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

    async def list_indexed_resources(
        self, module_id: Optional[str] = None
    ) -> list[tuple[str, str]]:
        """RAG-16D rebuild source list: (module_id, resource_id) pairs for
        every indexed resource that is NOT tombstoned — PostgreSQL is the
        rebuild authority, never SQLite and never Qdrant."""
        if not self._healthy or not self._conn:
            return []
        try:
            async with self._conn.cursor() as cur:
                if module_id:
                    await cur.execute(
                        """SELECT s.module_id, s.resource_id
                           FROM gptbridge_rag.index_state s
                           WHERE s.status = 'indexed'
                             AND s.module_id = %s
                             AND NOT EXISTS (
                                 SELECT 1 FROM gptbridge_rag.tombstone t
                                 WHERE t.module_id = s.module_id
                                   AND t.resource_id = s.resource_id
                                   AND t.purged IS NOT TRUE)
                           ORDER BY s.module_id, s.resource_id""",
                        (module_id,),
                    )
                else:
                    await cur.execute(
                        """SELECT s.module_id, s.resource_id
                           FROM gptbridge_rag.index_state s
                           WHERE s.status = 'indexed'
                             AND NOT EXISTS (
                                 SELECT 1 FROM gptbridge_rag.tombstone t
                                 WHERE t.module_id = s.module_id
                                   AND t.resource_id = s.resource_id
                                   AND t.purged IS NOT TRUE)
                           ORDER BY s.module_id, s.resource_id"""
                    )
                rows = await cur.fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: list_indexed_resources failed: %s",
                exc,
            )
            return []

    async def list_index_state_details(
        self, module_id: Optional[str] = None
    ) -> Optional[list[dict[str, Any]]]:
        """§10.6 parity sweep: per-resource index_state detail for every
        non-tombstoned row (any status — status drift is itself a signal)."""
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                base = (
                    """SELECT s.resource_id, s.module_id, s.chunk_count,
                              s.content_hash, s.embedding_model, s.embedding_version,
                              s.source_revision, s.chunking_version, s.parser_version,
                              s.rag_schema_version, s.status
                       FROM gptbridge_rag.index_state s
                       WHERE NOT EXISTS (
                           SELECT 1 FROM gptbridge_rag.tombstone t
                           WHERE t.module_id = s.module_id
                             AND t.resource_id = s.resource_id
                             AND t.purged IS NOT TRUE)"""
                )
                if module_id:
                    await cur.execute(
                        base + " AND s.module_id = %s", (module_id,)
                    )
                else:
                    await cur.execute(base)
                rows = await cur.fetchall()
            return [
                {
                    "resource_id": str(r[0]),
                    "module_id": str(r[1]),
                    "chunk_count": int(r[2] or 0),
                    "content_hash": str(r[3] or ""),
                    "embedding_model": str(r[4] or ""),
                    "embedding_version": int(r[5] or 0),
                    "source_revision": int(r[6] or 1),
                    "chunking_version": int(r[7] or 1),
                    "parser_version": int(r[8] or 1),
                    "rag_schema_version": int(r[9] or 1),
                    "status": str(r[10] or ""),
                }
                for r in rows
            ]
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: list_index_state_details failed: %s",
                exc,
            )
            return None

    async def chunk_count_by_resource(
        self, module_id: Optional[str] = None
    ) -> Optional[dict[tuple[str, str], int]]:
        """§10.6 parity sweep: actual PG chunk rows per (module, resource)."""
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                if module_id:
                    await cur.execute(
                        """SELECT module_id, resource_id, COUNT(*)
                           FROM gptbridge_rag.chunk
                           WHERE module_id = %s
                           GROUP BY module_id, resource_id""",
                        (module_id,),
                    )
                else:
                    await cur.execute(
                        """SELECT module_id, resource_id, COUNT(*)
                           FROM gptbridge_rag.chunk
                           GROUP BY module_id, resource_id"""
                    )
                rows = await cur.fetchall()
            return {(str(r[0]), str(r[1])): int(r[2]) for r in rows}
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: chunk_count_by_resource failed: %s",
                exc,
            )
            return None

    # -- RAG-11: generation registry -------------------------------------------

    async def upsert_generation(self, generation: Any) -> bool:
        """Persist IndexGeneration to gptbridge_rag.generation."""
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO gptbridge_rag.generation
                          (generation_id, index_schema_version,
                           embedding_model, embedding_dimension,
                           chunk_policy_version, chunk_size, chunk_overlap,
                           created_at_utc, state, collection_name,
                           alias_name, previous_generation_id, points_count,
                           verification_result, error_message,
                           parser_version, vector_schema_version,
                           metadata_schema_version, policy_version,
                           activated_at_utc, retired_at_utc)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s)
                       ON CONFLICT (generation_id) DO UPDATE SET
                           state = EXCLUDED.state,
                           points_count = EXCLUDED.points_count,
                           verification_result = EXCLUDED.verification_result,
                           error_message = EXCLUDED.error_message,
                           activated_at_utc = EXCLUDED.activated_at_utc,
                           retired_at_utc = EXCLUDED.retired_at_utc""",
                    (
                        generation.generation_id,
                        generation.index_schema_version,
                        generation.embedding_model,
                        int(generation.embedding_dimension),
                        generation.chunk_policy_version,
                        int(generation.chunk_size),
                        int(generation.chunk_overlap),
                        generation.created_at,
                        generation.state.value
                        if hasattr(generation.state, "value")
                        else str(generation.state),
                        generation.collection_name,
                        generation.alias_name,
                        generation.previous_generation_id,
                        int(generation.points_count),
                        json.dumps(generation.verification_result)
                        if generation.verification_result is not None
                        else None,
                        generation.error_message,
                        generation.parser_version,
                        generation.vector_schema_version,
                        generation.metadata_schema_version,
                        generation.policy_version,
                        generation.activated_at,
                        generation.retired_at,
                    ),
                )
            return True
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: upsert_generation failed: %s",
                exc,
            )
            return False

    async def get_generation(self, generation_id: str) -> Optional[Any]:
        """Fetch one generation row as IndexGeneration."""
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT generation_id, index_schema_version,
                              embedding_model, embedding_dimension,
                              chunk_policy_version, chunk_size, chunk_overlap,
                              created_at_utc, state, collection_name,
                              alias_name, previous_generation_id,
                              points_count, verification_result,
                              error_message, parser_version,
                              vector_schema_version, metadata_schema_version,
                              policy_version, activated_at_utc, retired_at_utc
                       FROM gptbridge_rag.generation
                       WHERE generation_id = %s""",
                    (generation_id,),
                )
                row = await cur.fetchone()
            if row is None:
                return None
            return self._generation_row(row)
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: get_generation failed: %s", exc
            )
            return None

    async def get_active_generation(self, alias_name: str) -> Optional[Any]:
        """Fetch the single ACTIVE generation for an alias (enforced by
        the uq_rag_generation_active partial unique index)."""
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT generation_id, index_schema_version,
                              embedding_model, embedding_dimension,
                              chunk_policy_version, chunk_size, chunk_overlap,
                              created_at_utc, state, collection_name,
                              alias_name, previous_generation_id,
                              points_count, verification_result,
                              error_message, parser_version,
                              vector_schema_version, metadata_schema_version,
                              policy_version, activated_at_utc, retired_at_utc
                       FROM gptbridge_rag.generation
                       WHERE alias_name = %s AND state = 'ACTIVE'""",
                    (alias_name,),
                )
                row = await cur.fetchone()
            if row is None:
                return None
            return self._generation_row(row)
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: get_active_generation failed: %s",
                exc,
            )
            return None

    @staticmethod
    def _generation_row(row: Any) -> Any:
        from .generation import GenerationState, IndexGeneration

        result = row[13]
        if isinstance(result, str):
            result = json.loads(result)
        return IndexGeneration(
            generation_id=row[0],
            index_schema_version=row[1],
            embedding_model=row[2],
            embedding_dimension=int(row[3]),
            chunk_policy_version=row[4],
            chunk_size=int(row[5]),
            chunk_overlap=int(row[6]),
            created_at=row[7],
            state=GenerationState(str(row[8])),
            collection_name=row[9],
            alias_name=row[10],
            previous_generation_id=row[11],
            points_count=int(row[12] or 0),
            verification_result=result,
            error_message=row[14],
            parser_version=row[15] or "v1",
            vector_schema_version=row[16] or "v1",
            metadata_schema_version=row[17] or "v1",
            policy_version=row[18] or "v1",
            activated_at=row[19],
            retired_at=row[20],
        )

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

    # -- RAG-08: canonical outbox_event --------------------------------------
    # gptbridge_rag.outbox_event is THE canonical outbox (A486/A487):
    # metadata + index_state + outbox commit in ONE transaction; Qdrant is
    # never part of that transaction — a worker applies events afterwards.

    async def document_write_tx(
        self,
        *,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        embedding_model: str,
        outbox_event: dict[str, Any],
    ) -> bool:
        """Atomic canonical write: resource + chunks + outbox event in ONE
        PostgreSQL transaction.  Qdrant is deliberately excluded — it is
        applied by the outbox worker after commit."""
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.transaction():
                if not await self.ensure_resource(document):
                    raise RuntimeError("ensure_resource failed")
                if not await self.replace_document_chunks(
                    resource_id=str(document["resource_id"]),
                    module_id=str(document["module_id"]),
                    embedding_model=embedding_model,
                    chunks=chunks,
                ):
                    raise RuntimeError("replace_document_chunks failed")
                await self._insert_outbox_event(outbox_event)
            return True
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: document_write_tx failed: %s", exc
            )
            return False

    async def _insert_outbox_event(self, event: dict[str, Any]) -> None:
        """INSERT into gptbridge_rag.outbox_event on the managed connection —
        joins any surrounding ``conn.transaction()`` block."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO gptbridge_rag.outbox_event
                      (event_id, request_id, operation, module_id, resource_id,
                       source_version, content_hash, generation_id, payload,
                       state, attempt_count, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                           'PENDING', 0, now(), now())""",
                (
                    event["event_id"],
                    event["request_id"],
                    event["operation"],
                    event["module_id"],
                    event["resource_id"],
                    int(event.get("source_version") or 0),
                    str(event.get("content_hash") or ""),
                    str(event.get("generation_id") or ""),
                    json.dumps(event.get("payload") or {}),
                ),
            )

    async def insert_outbox_event(self, event: dict[str, Any]) -> bool:
        """Standalone event insert (own statement; use document_write_tx for
        the atomic write path)."""
        if not self._healthy or not self._conn:
            return False
        try:
            await self._insert_outbox_event(event)
            return True
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: insert_outbox_event failed: %s",
                exc,
            )
            return False

    async def fetch_outbox_events(self, limit: int = 25) -> list[dict[str, Any]]:
        """Lease PENDING + due-RETRY events (SKIP LOCKED)."""
        if not self._healthy or not self._conn:
            return []
        try:
            async with self._conn.cursor() as cur:
                # Atomic lease: one statement claims rows as PROCESSING so
                # concurrent workers can never take the same event.
                await cur.execute(
                    """UPDATE gptbridge_rag.outbox_event
                       SET state = 'PROCESSING', updated_at = now()
                       WHERE event_id IN (
                           SELECT event_id FROM gptbridge_rag.outbox_event
                           WHERE state = 'PENDING'
                              OR (state = 'RETRY' AND next_retry_at <= now())
                           ORDER BY created_at
                           LIMIT %s
                           FOR UPDATE SKIP LOCKED
                       )
                       RETURNING event_id, request_id, operation, module_id,
                                 resource_id, source_version, content_hash,
                                 generation_id, payload, attempt_count""",
                    (limit,),
                )
                rows = await cur.fetchall()
            return [
                {
                    "event_id": str(r[0]), "request_id": r[1],
                    "operation": r[2], "module_id": r[3],
                    "resource_id": r[4], "source_version": r[5],
                    "content_hash": r[6], "generation_id": r[7],
                    "payload": r[8] if isinstance(r[8], dict) else {},
                    "attempt_count": int(r[9] or 0),
                }
                for r in rows
            ]
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: fetch_outbox_events failed: %s", exc
            )
            return []

    async def mark_outbox(
        self,
        event_id: str,
        state: str,
        *,
        error: Optional[str] = None,
        next_retry_at: Optional[str] = None,
        terminal: bool = False,
    ) -> bool:
        """Transition an outbox event; terminal states stamp completed_at."""
        if not self._healthy or not self._conn:
            return False
        completed = ", completed_at = now()" if terminal else ""
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    f"""UPDATE gptbridge_rag.outbox_event
                        SET state = %s, attempt_count = attempt_count + 1,
                            last_error = %s, next_retry_at = %s,
                            updated_at = now(){completed}
                        WHERE event_id = %s""",
                    (state, error, next_retry_at, event_id),
                )
            return True
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: mark_outbox failed: %s", exc
            )
            return False

    async def mark_outbox_succeeded(self, event_ids: Sequence[str]) -> int:
        """Batch terminal SUCCEEDED transition — one UPDATE per drain pass
        instead of one per event (G102 N+1 fix).  Returns rows updated."""
        if not self._healthy or not self._conn or not event_ids:
            return 0
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """UPDATE gptbridge_rag.outbox_event
                       SET state = 'SUCCEEDED',
                           attempt_count = attempt_count + 1,
                           last_error = NULL, next_retry_at = NULL,
                           updated_at = now(), completed_at = now()
                       WHERE event_id = ANY(%s)""",
                    ([str(e) for e in event_ids],),
                )
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: mark_outbox_succeeded failed: %s",
                exc,
            )
            return 0

    async def outbox_stats(self) -> dict[str, int]:
        """Outbox backlog grouped by state — DEAD_LETTER stays observable."""
        result = {"pending": 0, "processing": 0, "retry": 0,
                  "succeeded": 0, "dead_letter": 0}
        if not self._healthy or not self._conn:
            return result
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT state, COUNT(*) FROM gptbridge_rag.outbox_event
                       GROUP BY state"""
                )
                for state, count in await cur.fetchall():
                    key = str(state).lower()
                    if key in result:
                        result[key] = int(count)
            return result
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: outbox_stats failed: %s", exc
            )
            return result

    # -- RAG-09: reconciliation status surface --------------------------------

    async def reconciliation_status(self) -> dict[str, Any]:
        """Aggregated pending_rag_mutation status for the health surface."""
        if not self._healthy or not self._conn:
            return {"required": False, "pending_count": 0}
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT
                          COUNT(*) FILTER (WHERE status IN
                              ('pending','leased','reconciling')),
                          COUNT(*) FILTER (WHERE status = 'failed'),
                          COUNT(*) FILTER (WHERE status = 'dead_letter'),
                          MAX(canonical_synced_at),
                          MIN(created_at) FILTER (WHERE status = 'pending'),
                          (SELECT last_error FROM gptbridge_rag.reconciliation_queue
                            WHERE last_error IS NOT NULL
                            ORDER BY created_at DESC LIMIT 1)
                       FROM gptbridge_rag.reconciliation_queue"""
                )
                row = await cur.fetchone()
            pending = int(row[0] or 0)
            oldest = row[4]
            age = None
            if oldest is not None:
                try:
                    age = (
                        datetime.now(timezone.utc) - oldest
                    ).total_seconds() if hasattr(oldest, "tzinfo") else None
                except Exception:
                    age = None
            return {
                "required": pending > 0,
                "pending_count": pending,
                "processing_count": 0,
                "failed_count": int(row[1] or 0) + int(row[2] or 0),
                "last_success_at": row[3].isoformat() if row[3] else None,
                "last_error": row[5],
                "oldest_pending_age": age,
            }
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: reconciliation_status failed: %s",
                exc,
            )
            return {"required": False, "pending_count": 0}

    # -- RAG-10: provenance staleness ------------------------------------------

    async def mark_derived_stale(
        self, module_id: str, source_resource_id: str
    ) -> int:
        """A374 provenance: when a source is deleted/updated, derived
        resources must not remain canonical evidence — mark them STALE."""
        if not self._healthy or not self._conn:
            return 0
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """UPDATE gptbridge_rag.index_state
                          SET status = 'stale', indexed_at = now()
                        WHERE module_id = %s
                          AND resource_id IN (
                              SELECT DISTINCT derived_resource_id
                              FROM gptbridge_rag.provenance
                              WHERE source_resource_id = %s)
                          AND status = 'indexed'""",
                    (module_id, source_resource_id),
                )
                return int(cur.rowcount or 0)
        except Exception as exc:
            _logger.warning(
                "PostgreSQLMetadataAuthority: mark_derived_stale failed: %s", exc
            )
            return 0

    async def mark_index_state(self, module_id: str, resource_id: str,
                               state: str) -> bool:
        """Explicit index_state transition (DELETED after verified purge,
        STALE on supersede, etc.)."""
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """UPDATE gptbridge_rag.index_state
                          SET status = %s, indexed_at = now()
                        WHERE module_id = %s AND resource_id = %s""",
                    (state, module_id, resource_id),
                )
            return True
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: mark_index_state failed: %s", exc
            )
            return False

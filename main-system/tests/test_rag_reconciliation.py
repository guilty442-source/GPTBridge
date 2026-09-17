"""A374 reconciliation data-flow tests.

pending_rag_mutation replay: re-fetch owning-module content → re-chunk →
re-embed via the governed local runtime (2560d) → Qdrant upsert/delete →
PostgreSQL index_state → verify → mark reconciled.  Degraded-dimension
vectors are never replayed into the canonical collection.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from core_system.rag.pipeline import CanonicalRagPipeline
from core_system.rag.rag_qdrant import RagPipelineConfig
from core_system.rag.runtime_state import RagRuntimeState


class _FakeQdrant:
    def __init__(self) -> None:
        self.healthy = True
        self.points: list[Any] = []
        self.deleted: list[tuple[str, str]] = []

    def is_healthy(self) -> bool:
        return self.healthy

    def points_count(self) -> int:
        return len(self.points)

    async def ensure_collection(self, dimension: Optional[int] = None) -> bool:
        return True

    async def upsert_points(self, points: list[Any]) -> bool:
        self.points.extend(points)
        return True

    async def delete_resource(self, module_id: str, resource_id: str) -> bool:
        self.deleted.append((module_id, resource_id))
        return True


class _FakePostgres:
    def __init__(self, dimension: int = 2560) -> None:
        self.healthy = True
        self.dimension = dimension
        self.resources: list[dict[str, Any]] = []
        self.index_states: dict[tuple[str, str], Any] = {}
        self.chunk_counts: dict[tuple[str, str], int] = {}
        self.tombstones: list[dict[str, Any]] = []
        self.reconciled: list[str] = []
        self.dead_letters: list[str] = []

    def is_healthy(self) -> bool:
        return self.healthy

    async def ensure_resource(self, document: dict[str, Any]) -> bool:
        self.resources.append(document)
        return True

    async def replace_document_chunks(self, **kwargs: Any) -> bool:
        self.chunk_counts[
            (kwargs["module_id"], kwargs["resource_id"])
        ] = len(kwargs["chunks"])
        return True

    async def upsert_index_state(self, state: Any, **kwargs: Any) -> bool:
        self.index_states[(state.module_id, state.resource_id)] = state
        self.chunk_counts[(state.module_id, state.resource_id)] = int(
            kwargs.get("chunk_count") or 0
        )
        return True

    async def get_index_state(self, module_id: str, resource_id: str) -> Any:
        return self.index_states.get((module_id, resource_id))

    async def is_tombstoned(self, module_id: str, resource_id: str) -> bool:
        return False

    async def raise_tombstone(self, **kwargs: Any) -> bool:
        self.tombstones.append(kwargs)
        return True

    async def enqueue_reconciliation(self, item: Any) -> bool:
        return True

    async def mark_reconciled(self, operation_id: str) -> bool:
        self.reconciled.append(operation_id)
        return True

    async def mark_recon_retry(self, *args: Any) -> bool:
        return True

    async def dead_letter_reconciliation(self, operation_id: str, *_: Any) -> bool:
        self.dead_letters.append(operation_id)
        return True

    async def drain_reconciled(self) -> int:
        return 0

    async def index_state_summary(
        self, embedding_model: str, embedding_dimension: int
    ) -> dict[str, int]:
        return {
            "documents": len(self.index_states),
            "total_chunks": sum(self.chunk_counts.values()),
            "missing_point_ids": 0,
            "missing_hashes": 0,
            "mismatched_versions": 0,
        }


class _FakeDeletionCoordinator:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict[str, Any]] = []

    async def reconcile_tombstone_async(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            ok=self.ok, reason=None if self.ok else "refused"
        )


def _pipeline(
    *,
    content: str,
    vector_dim: int = 2560,
    fetcher: Any = None,
) -> tuple[CanonicalRagPipeline, _FakeQdrant, _FakePostgres]:
    cfg = RagPipelineConfig(
        qdrant_url="http://unused", qdrant_api_key=None,
        collection_name="gptbridge_shared_knowledge",
        postgresql_dsn="postgresql://unused",
    )
    pipe = CanonicalRagPipeline(
        cfg,
        document_fetcher=fetcher
        or (lambda module_id, locator_id: {
            "content": content, "title": "Doc", "source": "/x.md",
            "document_id": "d1",
        }),
        embed_texts=lambda texts: [[0.1] * vector_dim for _ in texts],
    )
    qdrant, pg = _FakeQdrant(), _FakePostgres()
    pipe.qdrant, pipe.postgresql = qdrant, pg
    pipe._initialized = True
    pipe._state_machine.evaluate_startup(
        qdrant_healthy=False, postgresql_healthy=False, index_state_matches=False
    )
    return pipe, qdrant, pg


async def _enqueue(pipe: CanonicalRagPipeline, operation: str = "update") -> None:
    await pipe._enqueue_degraded_write(
        module_id="xingcheng",
        resource_id="doc-abc",
        locator_id="xingcheng:doc-abc",
        source_revision=1,
        content_hash="h" * 64,
        operation=operation,
    )


@pytest.mark.asyncio
async def test_pending_mutation_replays_full_flow() -> None:
    pipe, qdrant, pg = _pipeline(content="x" * 3000)
    await _enqueue(pipe)
    assert pipe.state == RagRuntimeState.DEGRADED

    state = await pipe.attempt_recovery()

    assert state == RagRuntimeState.CANONICAL
    assert qdrant.points, "re-chunked content must reach Qdrant"
    assert all(len(p.vector) == 2560 for p in qdrant.points)
    assert pg.index_states, "index_state writeback required"
    assert pg.reconciled, "canonical queue row must be stamped"
    assert pipe._queue.is_complete()


@pytest.mark.asyncio
async def test_degraded_dimension_vectors_never_replayed() -> None:
    # A 256-dim degraded hashing vector must be rejected, never upserted.
    pipe, qdrant, pg = _pipeline(content="x" * 3000, vector_dim=256)
    await _enqueue(pipe)

    state = await pipe.attempt_recovery()

    assert state == RagRuntimeState.DEGRADED
    assert pipe.state_machine.effective_state == "RECONCILIATION_FAILED"
    assert qdrant.points == [], "wrong-dimension vectors must not reach Qdrant"
    assert pipe.state_machine.reconciliation_required is True


@pytest.mark.asyncio
async def test_tombstone_replay_deletes_vectors() -> None:
    pipe, qdrant, pg = _pipeline(content="x")
    await _enqueue(pipe, operation="tombstone")

    state = await pipe.attempt_recovery()

    assert state == RagRuntimeState.CANONICAL
    assert qdrant.deleted == [("xingcheng", "doc-abc")]
    assert pg.tombstones and pg.tombstones[0]["content_hash"] == "h" * 64


@pytest.mark.asyncio
async def test_missing_source_retries_then_dead_letters() -> None:
    pipe, qdrant, pg = _pipeline(
        content="", fetcher=lambda module_id, locator_id: None
    )
    await _enqueue(pipe)

    state = await pipe.attempt_recovery()

    assert state == RagRuntimeState.DEGRADED
    assert pipe.state_machine.effective_state == "RECONCILIATION_FAILED"
    # First lease attempt consumed → retry scheduled, item still pending.
    assert pipe._queue.pending_count() == 1


@pytest.mark.asyncio
async def test_tombstone_replay_routes_through_deletion_coordinator() -> None:
    pipe, qdrant, pg = _pipeline(content="x")
    coordinator = _FakeDeletionCoordinator()
    pipe._deletion_coordinator = coordinator
    await _enqueue(pipe, operation="tombstone")

    state = await pipe.attempt_recovery()

    assert state == RagRuntimeState.CANONICAL
    assert coordinator.calls == [{
        "module_id": "xingcheng",
        "resource_id": "doc-abc",
        "source_revision": 1,
        "content_hash": "h" * 64,
        "reason": "reconciled-tombstone",
    }]
    assert qdrant.deleted == [], "coordinator owns the vector delete"
    assert pg.tombstones == [], "coordinator owns the tombstone raise"


@pytest.mark.asyncio
async def test_tombstone_replay_coordinator_refusal_stays_degraded() -> None:
    pipe, qdrant, pg = _pipeline(content="x")
    pipe._deletion_coordinator = _FakeDeletionCoordinator(ok=False)
    await _enqueue(pipe, operation="tombstone")

    state = await pipe.attempt_recovery()

    assert state == RagRuntimeState.DEGRADED
    assert pipe.state_machine.effective_state == "RECONCILIATION_FAILED"
    assert qdrant.deleted == []
    assert pg.tombstones == []


@pytest.mark.asyncio
async def test_deletion_coordinator_runtime_completes_tombstone_replay() -> None:
    """The real runtime entry, fed the pipeline's fake interfaces."""
    from shared_layer.database.deletion_coordinator import (
        DeletionCoordinatorRuntime,
    )

    pipe, qdrant, pg = _pipeline(content="x")
    pipe._deletion_coordinator = DeletionCoordinatorRuntime(
        qdrant=qdrant, tombstone_raiser=pg
    )
    await _enqueue(pipe, operation="tombstone")

    state = await pipe.attempt_recovery()

    assert state == RagRuntimeState.CANONICAL
    assert qdrant.deleted == [("xingcheng", "doc-abc")]
    assert pg.tombstones and pg.tombstones[0]["content_hash"] == "h" * 64


def test_queue_item_carries_pending_mutation_fields() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    from core_system.rag.runtime_state import (
        RagRuntimeStateMachine,
        ReconciliationQueue,
    )

    machine = RagRuntimeStateMachine(ReconciliationQueue(conn))
    item = machine.enqueue_degraded_mutation(
        module_id="xingcheng", resource_id="doc-1",
        locator_id="xingcheng:doc-1", source_revision=3,
        content_hash="c" * 64, operation="update",
        embedding_model="qwen3-embedding:4b",
    )
    row = machine._queue.all_items()[0]
    assert row.operation == "update"
    assert row.resource_id == "doc-1"
    assert row.content_hash == "c" * 64
    assert row.source_revision == 3
    assert row.degraded_indexed_at
    assert row.canonical_synced_at is None
    assert row.retry_count == 0

    machine._queue.lease()
    row = machine._queue.all_items()[0]
    assert row.retry_count == 1

    machine._queue.mark_verified(item.operation_id)
    row = machine._queue.all_items()[0]
    assert row.canonical_synced_at

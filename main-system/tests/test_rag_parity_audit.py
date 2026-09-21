"""§10.6 RAG parity sweep tests.

The periodic sweep compares per-resource PostgreSQL authority state
(index_state + actual chunk rows) against filtered Qdrant point counts and
embedding versions; drifted resources are enqueued into the durable
reconciliation_queue — never repaired by deleting collections.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from core_system.rag.parity_audit import RagParityAudit


class _FakePostgres:
    def __init__(self) -> None:
        self.healthy = True
        self.details: list[dict[str, Any]] = []
        self.chunk_counts: dict[tuple[str, str], int] = {}
        self.enqueued: list[Any] = []
        self.fail_details = False

    async def list_index_state_details(
        self, module_id: Optional[str] = None
    ) -> Optional[list[dict[str, Any]]]:
        if self.fail_details:
            return None
        rows = self.details
        if module_id:
            rows = [r for r in rows if r["module_id"] == module_id]
        return rows

    async def chunk_count_by_resource(
        self, module_id: Optional[str] = None
    ) -> Optional[dict[tuple[str, str], int]]:
        if module_id:
            return {k: v for k, v in self.chunk_counts.items() if k[0] == module_id}
        return self.chunk_counts

    async def enqueue_reconciliation(self, item: Any) -> bool:
        if any(i.idempotency_key == item.idempotency_key for i in self.enqueued):
            return True  # ON CONFLICT dedup
        self.enqueued.append(item)
        return True


class _FakeQdrant:
    def __init__(self) -> None:
        self.counts: dict[tuple[str, str], Optional[int]] = {}
        self.delete_calls: list[tuple[str, str]] = []

    def count_resource_points(
        self, module_id: str, resource_id: str, generation_id: Optional[str] = None
    ) -> Optional[int]:
        return self.counts.get((module_id, resource_id))


class _Config:
    embedding_model = "qwen3-embedding:4b"
    embedding_version = 1
    embedding_dimension = 2560
    chunk_size = 512
    chunk_overlap = 64


def _row(
    module_id: str = "mod",
    resource_id: str = "res-1",
    chunk_count: int = 3,
    content_hash: str = "abc123",
    embedding_model: str = "qwen3-embedding:4b",
    embedding_version: int = 1,
    source_revision: int = 7,
    status: str = "indexed",
) -> dict[str, Any]:
    return {
        "resource_id": resource_id,
        "module_id": module_id,
        "chunk_count": chunk_count,
        "content_hash": content_hash,
        "embedding_model": embedding_model,
        "embedding_version": embedding_version,
        "source_revision": source_revision,
        "chunking_version": 1,
        "parser_version": 1,
        "rag_schema_version": 1,
        "status": status,
    }


def _rig() -> tuple[_FakePostgres, _FakeQdrant, RagParityAudit]:
    pg = _FakePostgres()
    qd = _FakeQdrant()
    return pg, qd, RagParityAudit(pg, qd, _Config())


@pytest.mark.asyncio
async def test_sweep_clean_resources_no_enqueue():
    pg, qd, audit = _rig()
    pg.details = [_row()]
    pg.chunk_counts = {("mod", "res-1"): 3}
    qd.counts = {("mod", "res-1"): 3}
    report = await audit.sweep()
    assert report["checked"] == 1
    assert report["drifted"] == 0
    assert report["enqueued"] == 0
    assert not pg.enqueued


@pytest.mark.asyncio
async def test_sweep_point_count_mismatch_enqueues_only_drifted():
    pg, qd, audit = _rig()
    pg.details = [_row(resource_id="res-ok"), _row(resource_id="res-bad")]
    pg.chunk_counts = {("mod", "res-ok"): 3, ("mod", "res-bad"): 3}
    qd.counts = {("mod", "res-ok"): 3, ("mod", "res-bad"): 1}
    report = await audit.sweep()
    assert report["drifted"] == 1
    assert report["enqueued"] == 1
    assert len(pg.enqueued) == 1
    item = pg.enqueued[0]
    assert item.resource_id == "res-bad"
    assert "point-count-mismatch" in item.payload["parity_reasons"]


@pytest.mark.asyncio
async def test_sweep_pg_internal_mismatch_and_embedding_drift():
    pg, qd, audit = _rig()
    pg.details = [
        _row(resource_id="r1", chunk_count=5),
        _row(resource_id="r2", embedding_model="old-model"),
    ]
    pg.chunk_counts = {("mod", "r1"): 3, ("mod", "r2"): 3}
    qd.counts = {("mod", "r1"): 3, ("mod", "r2"): 3}
    report = await audit.sweep()
    assert report["drifted"] == 2
    reasons = {d["resource_id"]: d["reasons"] for d in report["drifts"]}
    assert reasons["r1"] == ["pg-chunk-count-mismatch"]
    assert reasons["r2"] == ["embedding-version-drift"]


@pytest.mark.asyncio
async def test_sweep_missing_hash_and_status_flagged():
    pg, qd, audit = _rig()
    pg.details = [
        _row(resource_id="r1", content_hash=""),
        _row(resource_id="r2", status="reconcile_required"),
    ]
    pg.chunk_counts = {("mod", "r1"): 3, ("mod", "r2"): 3}
    qd.counts = {("mod", "r1"): 3, ("mod", "r2"): 3}
    report = await audit.sweep()
    reasons = {d["resource_id"]: d["reasons"] for d in report["drifts"]}
    assert "missing-content-hash" in reasons["r1"]
    assert "status-not-indexed" in reasons["r2"]


@pytest.mark.asyncio
async def test_sweep_qdrant_unverifiable_enqueues_fail_closed():
    pg, qd, audit = _rig()
    pg.details = [_row()]
    pg.chunk_counts = {("mod", "res-1"): 3}
    qd.counts = {}  # count_resource_points returns None
    report = await audit.sweep()
    assert report["unverifiable"] == 1
    assert report["enqueued"] == 1
    assert "qdrant-unverifiable" in pg.enqueued[0].payload["parity_reasons"]


@pytest.mark.asyncio
async def test_sweep_pg_unavailable_returns_error():
    pg, qd, audit = _rig()
    pg.fail_details = True
    report = await audit.sweep()
    assert report["error"] == "postgresql-authority-unavailable"
    assert report["checked"] == 0


@pytest.mark.asyncio
async def test_sweep_idempotent_reenqueue():
    pg, qd, audit = _rig()
    pg.details = [_row()]
    pg.chunk_counts = {("mod", "res-1"): 3}
    qd.counts = {("mod", "res-1"): 0}
    first = await audit.sweep()
    second = await audit.sweep()
    assert first["enqueued"] == 1
    assert second["enqueued"] == 1
    assert len(pg.enqueued) == 1  # same idempotency key deduped


@pytest.mark.asyncio
async def test_sweep_module_scope_filter():
    pg, qd, audit = _rig()
    pg.details = [_row(module_id="a"), _row(module_id="b", resource_id="res-2")]
    pg.chunk_counts = {("a", "res-1"): 3, ("b", "res-2"): 3}
    qd.counts = {("a", "res-1"): 3, ("b", "res-2"): 3}
    report = await audit.sweep(module_id="a")
    assert report["checked"] == 1
    assert report["drifted"] == 0


@pytest.mark.asyncio
async def test_parity_sweep_never_deletes():
    """§10.6: sweep only enqueues — it must never call delete paths."""
    pg, qd, audit = _rig()
    pg.details = [_row()]
    pg.chunk_counts = {("mod", "res-1"): 3}
    qd.counts = {("mod", "res-1"): 0}
    await audit.sweep()
    assert not qd.delete_calls

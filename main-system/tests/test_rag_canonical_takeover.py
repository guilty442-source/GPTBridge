"""Canonical Takeover Phase 1 — pipeline-level contract tests.

Covers the takeover invariants at the canonical boundary:

  RAG-01  embedding contract qwen3-embedding:4b / 2560 (config level)
  RAG-02  gateway surface states + BLOCKED on hard contract violations
  RAG-04  read barrier: index_state proof, scope, tombstone, identity
  RAG-09  Qdrant payload contract: no content/text/path/physical_location
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from core_system.rag.pipeline import CanonicalRagPipeline
from core_system.rag.rag_qdrant import (
    FORBIDDEN_PAYLOAD_FIELDS,
    IndexState,
    QdrantCanonicalRuntime,
    RagPipelineConfig,
    is_loopback_url,
    sanitize_payload,
)
from core_system.rag.runtime_state import RagRuntimeState


def _config(**overrides: Any) -> RagPipelineConfig:
    base = dict(
        qdrant_url="http://127.0.0.1:6333",
        qdrant_api_key=None,
        collection_name="gptbridge_shared_knowledge",
        postgresql_dsn="postgresql://unused",
    )
    base.update(overrides)
    return RagPipelineConfig(**base)


# ---------------------------------------------------------------------------
# RAG-01: canonical embedding contract
# ---------------------------------------------------------------------------


def test_canonical_embedding_contract_is_qwen3_2560() -> None:
    cfg = _config()
    assert cfg.embedding_model == "qwen3-embedding:4b"
    assert cfg.embedding_dimension == 2560
    assert cfg.embedding_provider == "ollama"


def test_openai_provider_defaults_match_canonical_contract() -> None:
    """No code path may default to text-embedding-3-small / 1536."""
    import inspect

    from core_system.rag import embeddings

    signature = inspect.signature(embeddings.OpenAIEmbeddingProvider.__init__)
    assert signature.parameters["model"].default == "qwen3-embedding:4b"
    assert signature.parameters["dimension"].default == 2560
    source = inspect.getsource(embeddings)
    assert "text-embedding-3-small" not in source
    assert "1536" not in source


# ---------------------------------------------------------------------------
# RAG-02: loopback-only Qdrant + gateway surface states
# ---------------------------------------------------------------------------


def test_qdrant_url_must_be_loopback() -> None:
    assert is_loopback_url("http://127.0.0.1:6333")
    assert is_loopback_url("http://localhost:6333")
    assert is_loopback_url("http://[::1]:6333")
    assert not is_loopback_url("http://8.8.8.8:6333")
    assert not is_loopback_url("https://qdrant.cloud.example:6333")
    assert not is_loopback_url("http://192.168.1.10:6333")


@pytest.mark.asyncio
async def test_remote_qdrant_url_is_rejected_before_connect() -> None:
    runtime = QdrantCanonicalRuntime(
        _config(qdrant_url="http://192.168.1.10:6333")
    )
    ok = await runtime.initialize()
    assert ok is False
    assert runtime.is_healthy() is False
    assert runtime.last_error.startswith("QDRANT_URL_NOT_LOOPBACK")
    assert runtime.client is None  # client never constructed


def _pipeline_surface_state(
    effective: str, blocked: Optional[str] = None
) -> str:
    pipe = CanonicalRagPipeline(_config())
    pipe._blocked_reason = blocked
    pipe._state_machine._state = {
        "STARTING": RagRuntimeState.STARTING,
        "CANONICAL": RagRuntimeState.CANONICAL,
        "DEGRADED": RagRuntimeState.DEGRADED,
        "RECONCILING": RagRuntimeState.RECONCILING,
    }[effective]
    return pipe.gateway_state()


def test_gateway_surface_states() -> None:
    assert _pipeline_surface_state("STARTING") == "BOOTSTRAPPING"
    assert _pipeline_surface_state("CANONICAL") == "CANONICAL_READY"
    assert _pipeline_surface_state("DEGRADED") == "DEGRADED_READY"
    assert _pipeline_surface_state("RECONCILING") == "RECONCILING"
    assert _pipeline_surface_state("CANONICAL", "INDEX_MISMATCH:x") == "BLOCKED"


# ---------------------------------------------------------------------------
# RAG-02/05: dimension mismatch -> INDEX_MISMATCH, collection never modified
# ---------------------------------------------------------------------------


def _collection_info(size: int) -> Any:
    vectors = MagicMock()
    vectors.size = size
    params = MagicMock()
    params.vectors = vectors
    config = MagicMock()
    config.params = params
    info = MagicMock()
    info.config = config
    return info


@pytest.mark.asyncio
async def test_existing_collection_dimension_mismatch_blocks() -> None:
    runtime = QdrantCanonicalRuntime(_config())
    client = MagicMock()
    existing = MagicMock()
    existing.name = "gptbridge_shared_knowledge"
    client.get_collections.return_value = MagicMock(collections=[existing])
    client.get_collection.return_value = _collection_info(1536)
    runtime.client = client
    runtime._healthy = True

    ok = await runtime.ensure_collection()

    assert ok is False
    assert runtime.collection_error.startswith("INDEX_MISMATCH")
    assert "dimension=1536" in runtime.collection_error
    client.create_collection.assert_not_called()  # never overwrite


@pytest.mark.asyncio
async def test_matching_dimension_passes_and_absent_collection_created() -> None:
    runtime = QdrantCanonicalRuntime(_config())
    client = MagicMock()
    existing = MagicMock()
    existing.name = "gptbridge_shared_knowledge"
    client.get_collections.return_value = MagicMock(collections=[existing])
    client.get_collection.return_value = _collection_info(2560)
    runtime.client = client
    runtime._healthy = True
    assert await runtime.ensure_collection() is True
    assert runtime.collection_error is None

    client.get_collections.return_value = MagicMock(collections=[])
    assert await runtime.ensure_collection() is True
    client.create_collection.assert_called_once()


# ---------------------------------------------------------------------------
# Fakes for pipeline-level tests
# ---------------------------------------------------------------------------


class _FakeQdrant:
    def __init__(self, hits: Optional[list[dict[str, Any]]] = None) -> None:
        self.healthy = True
        self.hits = hits or []
        self.points: list[Any] = []
        self.collection_error: Optional[str] = None
        self.last_error: Optional[str] = None
        self.search_calls: list[dict[str, Any]] = []

    def is_healthy(self) -> bool:
        return self.healthy

    def points_count(self) -> int:
        return len(self.points)

    async def initialize(self) -> bool:
        return self.healthy

    async def ensure_collection(self, dimension: Optional[int] = None) -> bool:
        return True

    async def upsert_points(self, points: list[Any]) -> bool:
        self.points.extend(points)
        return True

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(kwargs)
        return list(self.hits)


class _FakePostgres:
    def __init__(self) -> None:
        self.healthy = True
        self.index_states: dict[tuple[str, str], IndexState] = {}
        self.chunk_rows: dict[str, dict[str, Any]] = {}
        self.resources: list[dict[str, Any]] = []

    def is_healthy(self) -> bool:
        return self.healthy

    async def initialize(self) -> bool:
        return self.healthy

    async def pending_reconciliation_count(self) -> int:
        return 0

    async def get_index_state(self, module_id: str, resource_id: str) -> Any:
        return self.index_states.get((module_id, resource_id))

    async def fetch_chunks_for_points(
        self, module_ids: tuple[str, ...], point_ids: Any
    ) -> dict[str, dict[str, Any]]:
        return {
            pid: row
            for pid, row in self.chunk_rows.items()
            if pid in set(str(p) for p in point_ids)
            and row.get("module_id") in set(module_ids)
        }

    async def is_tombstoned(self, module_id: str, resource_id: str) -> bool:
        return False

    async def ensure_resource(self, document: dict[str, Any]) -> bool:
        self.resources.append(document)
        return True

    async def replace_document_chunks(self, **kwargs: Any) -> bool:
        return True

    async def upsert_index_state(self, state: Any, **kwargs: Any) -> bool:
        self.index_states[(state.module_id, state.resource_id)] = state
        return True

    async def fetch_metadata(
        self, module_id: str, resource_ids: list[str]
    ) -> dict[str, Any]:
        return {}

    async def fetch_document(self, *a: Any, **k: Any) -> None:
        return None

    async def keyword_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return []


def _state(resource_id: str, module_id: str = "xingcheng", status: str = "indexed") -> IndexState:
    return IndexState(
        resource_id=resource_id,
        module_id=module_id,
        embedding_model="qwen3-embedding:4b",
        embedding_dimension=2560,
        chunk_size=1200,
        chunk_overlap=200,
        indexed_at_utc="2026-01-01T00:00:00Z",
        content_hash="h",
        qdrant_point_id="pt",
        status=status,
    )


def _canonical_pipeline(
    hits: Optional[list[dict[str, Any]]] = None,
) -> tuple[CanonicalRagPipeline, _FakeQdrant, _FakePostgres]:
    pipe = CanonicalRagPipeline(_config())
    qdrant, pg = _FakeQdrant(hits), _FakePostgres()
    pipe.qdrant, pipe.postgresql = qdrant, pg
    pipe._initialized = True
    pipe._state_machine.evaluate_startup(
        qdrant_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    return pipe, qdrant, pg


def _hit(point_id: str, module_id: str = "xingcheng", resource_id: str = "doc-1") -> dict[str, Any]:
    return {
        "id": point_id,
        "score": 0.9,
        "payload": {
            "module_id": module_id,
            "document_resource_id": resource_id,
            "chunk_id": f"{resource_id}-0",
        },
    }


# ---------------------------------------------------------------------------
# RAG-04: canonical read barrier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barrier_drops_hits_without_pg_proof() -> None:
    hits = [
        _hit("pt-ok", resource_id="doc-ok"),
        _hit("pt-no-state", resource_id="doc-no-state"),
        _hit("pt-tombstoned", resource_id="doc-dead"),
        _hit("pt-no-chunk", resource_id="doc-no-chunk"),
        _hit("pt-conflict", resource_id="doc-other"),
        _hit("pt-scope", module_id="other-module", resource_id="doc-scope"),
    ]
    pipe, qdrant, pg = _canonical_pipeline(hits)
    pg.index_states[("xingcheng", "doc-ok")] = _state("doc-ok")
    pg.index_states[("xingcheng", "doc-dead")] = _state("doc-dead", status="tombstoned")
    pg.index_states[("xingcheng", "doc-no-chunk")] = _state("doc-no-chunk")
    pg.index_states[("xingcheng", "doc-other")] = _state("doc-other")
    pg.index_states[("other-module", "doc-scope")] = _state(
        "doc-scope", module_id="other-module"
    )
    pg.chunk_rows["pt-ok"] = {
        "chunk_id": "doc-ok-0", "resource_id": "doc-ok",
        "module_id": "xingcheng", "content": "canonical content",
        "title": "Doc", "source": "doc.md", "sequence": 0,
    }
    pg.chunk_rows["pt-conflict"] = {
        "chunk_id": "c", "resource_id": "doc-different",  # identity conflict
        "module_id": "xingcheng", "content": "x",
    }
    pg.chunk_rows["pt-scope"] = {
        "chunk_id": "c", "resource_id": "doc-scope",
        "module_id": "other-module", "content": "x",
    }

    results = await pipe.vector_search(
        [0.1] * 2560, module_ids=("xingcheng",), top_k=10
    )

    assert len(results) == 1
    assert results[0]["point_id"] == "pt-ok"
    assert results[0]["content"] == "canonical content"  # hydrated from PG
    # Filter pushdown: module scope was pushed into the Qdrant query.
    assert qdrant.search_calls[0]["module_ids"] == ("xingcheng",)


@pytest.mark.asyncio
async def test_vector_search_raises_when_blocked() -> None:
    pipe, _, _ = _canonical_pipeline()
    pipe._blocked_reason = "INDEX_MISMATCH:collection dimension=1536 expected=2560"
    with pytest.raises(RuntimeError, match="INDEX_MISMATCH"):
        await pipe.vector_search([0.1] * 2560, module_ids=("xingcheng",), top_k=5)
    assert pipe.gateway_state() == "BLOCKED"


# ---------------------------------------------------------------------------
# RAG-09: payload contract on the canonical write path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qdrant_payload_never_carries_forbidden_fields() -> None:
    pipe, qdrant, pg = _canonical_pipeline()
    document = {
        "module_id": "xingcheng",
        "resource_id": "doc-1",
        "document_id": "d1",
        "title": "Doc",
        "sha256": "h" * 64,
        "locator_id": "xingcheng:doc-1",
        "owner_id": "xingcheng",
        "data_category": "knowledge",
        "resource_type": "document",
        "resource_label": "Doc",
        "classification": "private",
        "version": 1,
    }
    chunks = [
        {
            "chunk_id": "d1-0",
            "sequence": 0,
            "character_start": 0,
            "character_end": 10,
            "content": "chunk text that must stay in PostgreSQL",
            "source": "E:\\secret\\doc.md",
            "point_id": "11111111-1111-1111-1111-111111111111",
            "payload": {
                "content": "leak attempt",
                "text": "leak",
                "path": "E:\\secret\\doc.md",
                "physical_location": "E:\\secret",
                "windows_path": "E:\\secret\\doc.md",
                "source": "E:\\secret\\doc.md",
                "module_id": "xingcheng",
                "document_resource_id": "doc-1",
                "chunk_id": "d1-0",
            },
        }
    ]
    ok = await pipe.index_document(
        document=document, chunks=chunks, vectors=[[0.1] * 2560]
    )
    assert ok is True
    assert len(qdrant.points) == 1
    payload = qdrant.points[0].payload
    for forbidden in FORBIDDEN_PAYLOAD_FIELDS:
        assert forbidden not in payload
    assert not any("path" in key or "location" in key for key in payload)
    # Filterable metadata survives.
    assert payload["module_id"] == "xingcheng"
    assert payload["document_resource_id"] == "doc-1"
    assert payload["chunk_id"] == "d1-0"


@pytest.mark.asyncio
async def test_index_document_blocked_never_writes() -> None:
    pipe, qdrant, pg = _canonical_pipeline()
    pipe._blocked_reason = "INDEX_MISMATCH:collection dimension=1536 expected=2560"
    with pytest.raises(RuntimeError, match="INDEX_MISMATCH"):
        await pipe.index_document(
            document={
                "module_id": "xingcheng",
                "resource_id": "doc-1",
                "locator_id": "x",
            },
            chunks=[],
            vectors=[],
        )
    assert qdrant.points == []


@pytest.mark.asyncio
async def test_blocked_never_recovers_via_attempt_recovery() -> None:
    pipe, _, _ = _canonical_pipeline()
    pipe._blocked_reason = "INDEX_MISMATCH:x"
    pipe._state_machine.report_canonical_failure("forced")
    assert pipe.state == RagRuntimeState.DEGRADED
    state = await pipe.attempt_recovery()
    assert state == RagRuntimeState.DEGRADED
    assert pipe.gateway_state() == "BLOCKED"


def test_sanitize_payload_strips_forbidden_and_path_like_keys() -> None:
    dirty = {
        "content": "x", "text": "x", "path": "x",
        "physical_location": "x", "windows_path": "x", "source": "x",
        "backup_path": "x", "install_location": "x",
        "module_id": "xingcheng", "chunk_id": "c1",
        "document_resource_id": "doc-1", "content_hash": "h",
    }
    clean = sanitize_payload(dirty)
    assert clean == {
        "module_id": "xingcheng",
        "chunk_id": "c1",
        "document_resource_id": "doc-1",
        "content_hash": "h",
    }


def test_forbidden_payload_fields_cover_codex_contract() -> None:
    assert FORBIDDEN_PAYLOAD_FIELDS >= {
        "content", "text", "path", "physical_location", "windows_path",
    }


# ---------------------------------------------------------------------------
# Scenario 3: PostgreSQL down — Qdrant alone is never "complete canonical"
# ---------------------------------------------------------------------------


def test_qdrant_alone_is_never_canonical() -> None:
    pipe = CanonicalRagPipeline(_config())
    pipe._initialized = True
    pipe.qdrant = _FakeQdrant()
    pipe.postgresql = _FakePostgres()
    pipe.postgresql.healthy = False
    assert pipe.is_ready() is False
    pipe._state_machine.evaluate_startup(
        qdrant_healthy=True, postgresql_healthy=False, index_state_matches=False
    )
    assert pipe.state == RagRuntimeState.DEGRADED
    assert pipe.gateway_state() == "DEGRADED_READY"


def test_startup_gate_never_reaches_canonical_without_both_stores() -> None:
    pipe = CanonicalRagPipeline(_config())
    pipe._state_machine.evaluate_startup(
        qdrant_healthy=True, postgresql_healthy=True, index_state_matches=False
    )
    assert pipe.state == RagRuntimeState.DEGRADED
    assert pipe.gateway_state() == "DEGRADED_READY"


# ---------------------------------------------------------------------------
# G50: production generation binding (A486/A487 wiring)
# ---------------------------------------------------------------------------

from core_system.rag.generation import (  # noqa: E402
    GenerationState,
    IndexGeneration,
)


def _active_gen(**overrides: Any) -> IndexGeneration:
    base: dict[str, Any] = dict(
        generation_id="gen-001",
        index_schema_version="v3",
        embedding_model="qwen3-embedding:4b",
        embedding_dimension=2560,
        chunk_policy_version="v3",
        chunk_size=1200,
        chunk_overlap=200,
        created_at="2026-01-01T00:00:00Z",
        state=GenerationState.ACTIVE,
        collection_name="col-gen-001",
        alias_name="gptbridge_shared_knowledge",
    )
    base.update(overrides)
    return IndexGeneration(**base)


class _FakePostgresWithGeneration(_FakePostgres):
    def __init__(self, generation: Optional[IndexGeneration] = None) -> None:
        super().__init__()
        self._generation = generation

    async def get_active_generation(self, alias_name: str) -> Any:
        return self._generation


@pytest.mark.asyncio
async def test_initialize_binds_active_generation() -> None:
    pipe = CanonicalRagPipeline(_config())
    qdrant = _FakeQdrant()
    qdrant.client = object()  # raw QdrantClient present post-initialize
    pipe.qdrant = qdrant
    pipe.postgresql = _FakePostgresWithGeneration(_active_gen())

    assert await pipe.initialize() is True
    assert pipe.generation_manager is not None
    assert pipe._active_generation == "gen-001"
    assert pipe._generation_rebuild_required is False
    assert pipe.state == RagRuntimeState.CANONICAL


@pytest.mark.asyncio
async def test_initialize_flags_drifted_generation_for_rebuild() -> None:
    """A fingerprint drift (e.g. embedding dimension) marks the ACTIVE
    generation stale: index_state no longer matches -> DEGRADED until a
    new generation is built/verified/activated."""
    pipe = CanonicalRagPipeline(_config())
    qdrant = _FakeQdrant()
    qdrant.client = object()
    pipe.qdrant = qdrant
    pipe.postgresql = _FakePostgresWithGeneration(
        _active_gen(embedding_dimension=1536)
    )

    assert await pipe.initialize() is True
    assert pipe._active_generation == "gen-001"
    assert pipe._generation_rebuild_required is True
    assert pipe.state == RagRuntimeState.DEGRADED


@pytest.mark.asyncio
async def test_initialize_without_generation_leaves_unbound() -> None:
    pipe = CanonicalRagPipeline(_config())
    qdrant = _FakeQdrant()
    qdrant.client = object()
    pipe.qdrant = qdrant
    pipe.postgresql = _FakePostgresWithGeneration(None)

    assert await pipe.initialize() is True
    assert pipe.generation_manager is not None
    assert pipe._active_generation is None
    assert pipe._generation_rebuild_required is False
    assert pipe.state == RagRuntimeState.CANONICAL

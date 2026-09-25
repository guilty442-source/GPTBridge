"""Rust vectord adapter contract tests (A610 target-primary takeover).

No live daemon required: the HTTP layer is stubbed so the tests pin the
adapter's request translation, fail-closed rules and backend selection.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core_system.rag.rag_qdrant import RagPipelineConfig
from core_system.rag.vector_models import (
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
)
from core_system.rag import rust_vector_runtime as rvr
from core_system.rag.rust_vector_runtime import (
    RustVectorRuntime,
    VectordClient,
    select_vector_runtime,
)


def _config(**overrides: Any) -> RagPipelineConfig:
    base = dict(
        qdrant_url="",
        qdrant_api_key=None,
        collection_name="col",
        postgresql_dsn="",
        embedding_dimension=4,
        vectord_url="http://127.0.0.1:8092",
        vectord_auto_start=False,
    )
    base.update(overrides)
    return RagPipelineConfig(**base)


class _StubTransport:
    """Records POST payloads and replays canned vectord responses."""

    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def __call__(self, url: str, path: str, payload: Any = None, timeout: float = 30.0):
        self.calls.append((path, payload or {}))
        return self.responses.get(path, {"ok": True})


def test_select_vector_runtime_defaults_to_rust() -> None:
    runtime = select_vector_runtime(_config())
    assert isinstance(runtime, RustVectorRuntime)


def test_select_vector_runtime_migration_window_qdrant() -> None:
    from core_system.rag.rag_qdrant import QdrantCanonicalRuntime

    runtime = select_vector_runtime(_config(vector_backend="qdrant"))
    assert type(runtime) is QdrantCanonicalRuntime


def test_non_loopback_vectord_url_blocked() -> None:
    runtime = RustVectorRuntime(_config(vectord_url="http://192.168.1.10:8092"))
    ok = asyncio.run(runtime.initialize())
    assert ok is False
    assert "NOT_LOOPBACK" in (runtime.last_error or "")


def test_filter_translation_must_should_must_not() -> None:
    query_filter = Filter(
        must=[FieldCondition(key="module_id", match=MatchAny(any=["m1", "m2"]))],
        should=[FieldCondition(key="resource_id", match=MatchValue(value="r1"))],
        must_not=[FieldCondition(key="archived", match=MatchValue(value=True))],
    )
    out = rvr._filter_to_json(query_filter)
    assert out["must"][0]["match"]["any"] == ["m1", "m2"]
    assert out["should"][0]["match"]["value"] == "r1"
    assert out["must_not"][0]["match"]["value"] is True


def test_client_search_request_shape(monkeypatch) -> None:
    stub = _StubTransport(
        {"/v1/search": {"ok": True, "hits": [{"id": "a", "score": 0.9, "payload": {}}]}}
    )
    monkeypatch.setattr(rvr, "_post", stub)
    client = VectordClient("http://127.0.0.1:8092/")
    response = client.query_points(
        collection_name="col",
        query=[0.1, 0.2, 0.3, 0.4],
        query_filter=Filter(
            must=[FieldCondition(key="module_id", match=MatchAny(any=["m1"]))]
        ),
        limit=5,
        score_threshold=0.5,
    )
    path, payload = stub.calls[0]
    assert path == "/v1/search"
    assert payload["collection"] == "col"
    assert payload["top_k"] == 5
    assert payload["score_threshold"] == 0.5
    assert payload["filter"]["must"][0]["key"] == "module_id"
    assert response.points[0].id == "a"
    assert response.points[0].score == 0.9


def test_client_upsert_translates_pointstruct(monkeypatch) -> None:
    stub = _StubTransport()
    monkeypatch.setattr(rvr, "_post", stub)
    client = VectordClient("http://127.0.0.1:8092")
    client.upsert(
        collection_name="col",
        points=[
            PointStruct(id="p1", vector=[1.0, 0.0, 0.0, 0.0],
                        payload={"module_id": "m1"}),
            {"id": "p2", "vector": [0.0, 1.0, 0.0, 0.0],
             "payload": {"module_id": "m1"}},
        ],
    )
    path, payload = stub.calls[0]
    assert path == "/v1/points/upsert"
    assert payload["points"][0] == {
        "id": "p1",
        "vector": [1.0, 0.0, 0.0, 0.0],
        "payload": {"module_id": "m1"},
    }
    assert payload["points"][1]["id"] == "p2"


def test_client_error_raises_fail_closed(monkeypatch) -> None:
    stub = _StubTransport(
        {"/v1/collections/list": {"ok": False, "error": "COLLECTION_MISSING:x"}}
    )
    monkeypatch.setattr(rvr, "_post", stub)
    client = VectordClient("http://127.0.0.1:8092")
    with pytest.raises(RuntimeError, match="COLLECTION_MISSING"):
        client.get_collections()


def test_alias_ops_translation(monkeypatch) -> None:
    stub = _StubTransport({"/v1/aliases/set": {"ok": True}})
    monkeypatch.setattr(rvr, "_post", stub)
    client = VectordClient("http://127.0.0.1:8092")
    client.create_alias(alias_name="alias", collection_name="col")
    path, payload = stub.calls[0]
    assert path == "/v1/aliases/set"
    assert payload == {"alias": "alias", "collection": "col"}


def test_collection_info_shape(monkeypatch) -> None:
    stub = _StubTransport(
        {"/v1/collections/info": {"ok": True, "points_count": 7, "dimension": 4}}
    )
    monkeypatch.setattr(rvr, "_post", stub)
    client = VectordClient("http://127.0.0.1:8092")
    info = client.get_collection("col")
    assert info.points_count == 7
    assert info.config.params.vectors.size == 4

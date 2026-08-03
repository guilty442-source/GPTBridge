from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))

from local_ai.application.local_rag import LocalRagService
from local_ai.infrastructure.qdrant_store import QdrantStore


class FakeRuntime:
    EMBEDDING_MODEL = "qwen3-embedding:4b"

    def __init__(self) -> None:
        self.models = {
            LocalRagService.ROUTER_MODEL,
            "qwen3.8:27b-q4_K_M",
            "gemma4:12b-it-qat",
            "qwen3-coder:30b-a3b-q4_K_M",
            "ornith-1.5:35b",
            "deepseek-r1:14b",
            "qwen3-vl:8b-thinking",
            LocalRagService.FALLBACK_MODEL,
        }
        self.generation_calls: list[dict[str, Any]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [
                float(sum(ord(character) for character in text) % 97 + 1),
                float(len(text) % 31 + 1),
                float(text.count("保固") * 20 + text.count("Python") * 15 + 1),
            ]
            for text in texts
        ]

    def selectable_models(self, *, refresh: bool = False) -> list[dict[str, str]]:
        return [{"name": model} for model in sorted(self.models)]

    def generate(self, **kwargs: Any) -> dict[str, Any]:
        self.generation_calls.append(dict(kwargs))
        if kwargs.get("requested_model") == LocalRagService.ROUTER_MODEL:
            return {"ok": True, "text": "general", "model": LocalRagService.ROUTER_MODEL}
        return {
            "ok": True,
            "text": "產品保固兩年。[R1]",
            "model": kwargs.get("requested_model"),
        }


class FakeQdrant:
    COLLECTION = QdrantStore.COLLECTION
    endpoint = "http://127.0.0.1:6333"

    def __init__(self) -> None:
        self.points: dict[str, dict[str, Any]] = {}
        self.vector_size = 0

    def ensure_collection(self, vector_size: int) -> None:
        if self.vector_size and self.vector_size != vector_size:
            raise RuntimeError("QDRANT_VECTOR_SIZE_MISMATCH")
        self.vector_size = vector_size

    def replace_document(
        self,
        document_id: str,
        points: list[dict[str, Any]],
        *,
        module_id: str | None = None,
    ) -> None:
        self.points = {
            key: value
            for key, value in self.points.items()
            if value["payload"]["document_id"] != document_id
        }
        self.points.update({str(point["id"]): point for point in points})

    def query(
        self,
        vector: list[float],
        *,
        limit: int,
        module_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        query_norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        records: list[dict[str, Any]] = []
        for point in self.points.values():
            target = point["vector"]
            target_norm = math.sqrt(sum(value * value for value in target)) or 1.0
            score = sum(left * right for left, right in zip(vector, target)) / (
                query_norm * target_norm
            )
            records.append({**point["payload"], "vector_score": score})
        records.sort(key=lambda item: -float(item["vector_score"]))
        return records[:limit]

    def status(self) -> dict[str, Any]:
        return {
            "available": True,
            "collection_exists": bool(self.vector_size),
            "endpoint": self.endpoint,
            "collection": self.COLLECTION,
            "point_count": len(self.points),
        }


class FakeRepository:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}
        self.chunks: list[dict[str, Any]] = []

    def existing_document(
        self, source: str, *, module_id: str = "local-ai"
    ) -> dict[str, Any] | None:
        return self.documents.get((module_id, source))

    def replace_document(
        self, *, document: dict[str, Any], chunks: list[dict[str, Any]]
    ) -> None:
        key = (str(document["module_id"]), str(document["source"]))
        self.documents[key] = {
            **document,
            "sha256": document["sha256"],
            "chunk_count": len(chunks),
        }
        self.chunks = [
            chunk
            for chunk in self.chunks
            if chunk.get("document_id") != document["document_id"]
        ]
        self.chunks.extend(
            {
                **chunk,
                "document_id": document["document_id"],
                "source": document["source"],
                "title": document["title"],
            }
            for chunk in chunks
        )

    def keyword_search(
        self, query: str, *, limit: int, module_ids: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        matches = [
            {**chunk, "keyword_score": 1.0}
            for chunk in self.chunks
            if query.casefold() in str(chunk.get("content") or "").casefold()
            and (not module_ids or chunk.get("module_id") in module_ids)
        ]
        return matches[:limit]

    def status(self) -> dict[str, Any]:
        return {"fts_enabled": True, "document_count": len(self.documents)}


class FakeReranker:
    def rerank(
        self, query: str, candidates: list[dict[str, Any]], *, size: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ranked = [
            {**item, "reranker_score": 1.0 if "保固" in item["content"] else 0.1}
            for item in candidates
        ]
        ranked.sort(key=lambda item: -item["reranker_score"])
        return ranked, {"applied": True, "model": f"fake-{size}"}

    def status(self) -> dict[str, Any]:
        return {"loaded": ["0.6b"], "local_files_only": True}


def build_rag(tmp_path: Path) -> tuple[LocalRagService, FakeRuntime, FakeQdrant]:
    runtime = FakeRuntime()
    qdrant = FakeQdrant()
    rag = LocalRagService(
        tmp_path / "local-ai",
        runtime,
        qdrant_store=qdrant,  # type: ignore[arg-type]
        reranker=FakeReranker(),  # type: ignore[arg-type]
        repository=FakeRepository(),  # type: ignore[arg-type]
    )
    return rag, runtime, qdrant


def test_ingest_writes_one_shared_qdrant_collection_and_fts5_index(tmp_path: Path) -> None:
    rag, _, qdrant = build_rag(tmp_path)

    result = rag.ingest(
        {
            "documents": [
                {"id": "policy", "title": "保固政策", "text": "本產品提供兩年保固。"},
                {"id": "code", "title": "程式指南", "text": "Python 請使用 pytest 執行測試。"},
            ]
        }
    )

    assert result["ok"] is True
    assert result["knowledge_base"] == "shared"
    assert result["available_to_all_local_models"] is True
    assert result["qdrant_collection"] == "gptbridge_shared_knowledge"
    assert len(qdrant.points) == 2
    assert all(point["payload"]["shared_knowledge_base"] for point in qdrant.points.values())
    assert rag.repository.status()["fts_enabled"] is True
    assert rag.repository.keyword_search(
        "保固", limit=5, module_ids=("local-ai",)
    )[0]["source"] == "policy"


def test_query_uses_hybrid_reranking_and_routed_model_with_citations(tmp_path: Path) -> None:
    rag, runtime, _ = build_rag(tmp_path)
    assert rag.ingest(
        {"documents": [{"id": "policy", "title": "保固政策", "text": "本產品提供兩年保固。"}]}
    )["ok"] is True

    result = rag.query({"question": "產品保固多久？", "rag_mode": "fast"})

    assert result["ok"] is True
    assert result["route"] == "fast"
    assert result["generation_model"] == "gemma4:12b-it-qat"
    assert result["citations"][0]["source"] == "policy"
    assert result["reranker"]["applied"] is True
    answer_call = runtime.generation_calls[-1]
    assert answer_call["requested_model"] == "gemma4:12b-it-qat"
    assert "<retrieved_context>" in answer_call["prompt"]
    assert "不可信資料" in answer_call["prompt"]


def test_all_rag_routes_read_the_same_knowledge_base(tmp_path: Path) -> None:
    rag, _, qdrant = build_rag(tmp_path)
    rag.ingest({"documents": [{"id": "shared", "text": "所有模型共用的知識。"}]})

    for mode, models in rag.RAG_MODELS.items():
        result = rag.query(
            {"question": "共用的知識是什麼？", "rag_mode": mode, "generate": False}
        )
        assert result["ok"] is True
        assert result["knowledge_base"] == "shared"
        assert result["citations"][0]["source"] == "shared"
        assert models
    assert qdrant.COLLECTION == "gptbridge_shared_knowledge"


def test_rag_rejects_governance_rule_paths(tmp_path: Path) -> None:
    rag, _, _ = build_rag(tmp_path)
    protected = tmp_path / "governance_rule"
    protected.mkdir()
    (protected / "secret.md").write_text("不可索引", encoding="utf-8")

    result = rag.ingest({"path": str(protected)})

    assert result["ok"] is False
    assert result["error_code"] == "RAG_DOCUMENTS_REQUIRED"
    assert result["errors"][0]["error"] == "RAG_GOVERNANCE_PATH_DENIED"


def test_qdrant_endpoint_must_be_loopback() -> None:
    try:
        QdrantStore(endpoint="https://example.com:6333")
    except ValueError as exc:
        assert str(exc) == "QDRANT_ENDPOINT_MUST_BE_LOOPBACK"
    else:
        raise AssertionError("non-loopback Qdrant endpoint was accepted")

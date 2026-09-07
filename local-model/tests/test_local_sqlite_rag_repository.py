from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from shared_layer.resource_identity import locator_id_for  # noqa: E402
from xingcheng.infrastructure.local_sqlite_rag_repository import (  # noqa: E402
    LocalSqliteRagRepository,
)


def _document(tmp_path: Path, *, source: str = "docs/guide.md") -> dict:
    document_id = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return {
        "document_id": "doc-abc123",
        "source": source,
        "title": "保固政策",
        "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "character_count": 26,
        "embedding_model": "qwen3-embedding:4b",
        "module_id": "xingcheng",
        "platform_id": "local-model-platform",
        "owner_id": "xingcheng",
        "data_category": "business",
        "resource_type": "document",
        "resource_id": f"doc-{document_id}",
        "resource_label": f"local-model-platform:xingcheng:business:document:doc-{document_id}",
        "locator_id": str(locator_id_for("xingcheng", f"doc-{document_id}")),
        "classification": "private",
        "version": 1,
    }


def _chunks() -> list[dict]:
    return [
        {
            "chunk_id": "doc-abc123-1",
            "sequence": 1,
            "character_start": 0,
            "character_end": 13,
            "content": "本產品提供兩年保固。",
            "resource_id": "chunk-1",
            "resource_label": "local-model-platform:xingcheng:business:chunk:chunk-1",
            "module_id": "xingcheng",
        },
        {
            "chunk_id": "doc-abc123-2",
            "sequence": 2,
            "character_start": 14,
            "character_end": 26,
            "content": "Python 請使用 pytest 執行測試。",
            "resource_id": "chunk-2",
            "resource_label": "local-model-platform:xingcheng:business:chunk:chunk-2",
            "module_id": "xingcheng",
        },
    ]


def test_replace_document_persists_metadata_and_chunks(tmp_path: Path) -> None:
    repository = LocalSqliteRagRepository(tmp_path / "xingcheng")

    repository.replace_document(document=_document(tmp_path), chunks=_chunks())

    existing = repository.existing_document("docs/guide.md", module_id="xingcheng")
    assert existing is not None
    expected_doc_id = hashlib.sha256(b"docs/guide.md").hexdigest()[:32]
    assert existing["document_id"] == f"doc-{expected_doc_id}"
    assert existing["sha256"].startswith("9f86d081")
    assert existing["title"] == "保固政策"
    assert existing["character_count"] == 26
    assert existing["chunk_count"] == 2
    assert existing["embedding_model"] == "qwen3-embedding:4b"
    assert existing["index_status"] == "indexed"


def test_keyword_search_returns_scored_matching_chunks(tmp_path: Path) -> None:
    repository = LocalSqliteRagRepository(tmp_path / "xingcheng")
    repository.replace_document(document=_document(tmp_path), chunks=_chunks())

    results = repository.keyword_search(
        "保固", limit=5, module_ids=("xingcheng",)
    )

    assert results
    assert results[0]["source"] == "docs/guide.md"
    assert results[0]["document_id"] == "doc-abc123"
    assert results[0]["content"] == "本產品提供兩年保固。"
    assert results[0]["keyword_score"] > 0
    assert all(item["module_id"] == "xingcheng" for item in results)


def test_keyword_search_respects_module_scope(tmp_path: Path) -> None:
    repository = LocalSqliteRagRepository(tmp_path / "xingcheng")
    repository.replace_document(document=_document(tmp_path), chunks=_chunks())

    results = repository.keyword_search(
        "保固", limit=5, module_ids=("other-module",)
    )

    assert results == []


def test_status_reports_counts_without_storing_physical_content(tmp_path: Path) -> None:
    repository = LocalSqliteRagRepository(tmp_path / "xingcheng")
    repository.replace_document(document=_document(tmp_path), chunks=_chunks())

    status = repository.status()

    assert status["engine"] == "local-sqlite3"
    assert status["schema"] == "local-rag-keywords"
    assert status["content_storage"] == "excluded-by-architecture"
    assert status["document_count"] == 1
    assert status["chunk_count"] == 2
    assert status["character_count"] == 26


def test_replace_document_overwrites_previous_version(tmp_path: Path) -> None:
    repository = LocalSqliteRagRepository(tmp_path / "xingcheng")
    document = _document(tmp_path)
    document["sha256"] = "old-digest"
    repository.replace_document(document=document, chunks=_chunks())
    document["sha256"] = "new-digest"
    document["version"] = 2
    repository.replace_document(document=document, chunks=_chunks())

    existing = repository.existing_document("docs/guide.md", module_id="xingcheng")
    assert existing["sha256"] == "new-digest"
    assert repository.status()["document_count"] == 1
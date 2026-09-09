from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from shared_layer.resource_identity import (
    PLATFORM_ID,
    ResourceIdentity,
    XINGCHENG_MODULE_ID,
    canonical_identifier,
    locator_id_for,
    point_id_for,
)


class LocalRagIndexMixin:
    """Embedding, dependency-error handling, and document ingestion for LocalRagService."""

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.EMBEDDING_BATCH_SIZE):
            batch = texts[start : start + self.EMBEDDING_BATCH_SIZE]
            embedded = self.transformer_runtime.embed(batch)
            if len(embedded) != len(batch):
                raise RuntimeError("RAG_EMBEDDING_COUNT_MISMATCH")
            vectors.extend([[float(value) for value in vector] for vector in embedded])
        return vectors

    @staticmethod
    def _point_id(chunk_id: str) -> str:
        return str(point_id_for(chunk_id))

    def _dependency_error(self, exc: Exception, *, indexed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        message = str(exc)
        embedding_unavailable = "RAG_EMBEDDING" in message.upper()
        return {
            "ok": False,
            "error_code": (
                "RAG_EMBEDDING_UNAVAILABLE"
                if embedding_unavailable
                else "RAG_VECTOR_STORE_UNAVAILABLE"
            ),
            "message": f"本地檢索相依服務不可用：{message}",
            "required_embedding_model": str(self.transformer_runtime.EMBEDDING_MODEL),
            "embedding_setup_command": f"ollama pull {self.transformer_runtime.EMBEDDING_MODEL}",
            "vector_database": self.vector_store.status(),
            "indexed": list(indexed or []),
            "network_used": False,
        }

    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        module_id = canonical_identifier(
            str(payload.get("module_id") or XINGCHENG_MODULE_ID),
            field="module_id",
        )
        documents, errors = self._documents(payload)
        if not documents:
            return {
                "ok": False,
                "error_code": "RAG_DOCUMENTS_REQUIRED",
                "message": "請提供 documents、path 或 paths；支援文字、程式碼、表格、HTML 與 DOCX。",
                "errors": errors,
            }
        indexed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        embedding_model = str(self.transformer_runtime.EMBEDDING_MODEL)
        try:
            for document in documents:
                digest = hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
                existing = self.repository.existing_document(
                    document["source"], module_id=module_id
                )
                if existing and existing.get("sha256") == digest and existing.get("embedding_model") == embedding_model:
                    skipped.append(
                        {"document_id": existing["document_id"], "source": document["source"], "reason": "unchanged"}
                    )
                    continue
                chunks = self._chunks(document["text"])
                vectors = self._embed([chunk["content"] for chunk in chunks])
                if not vectors or not vectors[0]:
                    raise RuntimeError("RAG_EMBEDDING_EMPTY")
                self.vector_store.ensure_collection(len(vectors[0]))
                document_id = hashlib.sha256(document["source"].encode("utf-8")).hexdigest()[:32]
                identity = ResourceIdentity(
                    module_id=module_id,
                    data_category="business",
                    resource_type="document",
                    resource_id=f"doc-{document_id}",
                )
                locator_id = str(
                    locator_id_for(module_id, identity.resource_id)
                )
                prepared: list[dict[str, Any]] = []
                points: list[dict[str, Any]] = []
                for chunk, vector in zip(chunks, vectors):
                    chunk_id = f"{document_id}-{chunk['sequence']}"
                    chunk_identity = ResourceIdentity(
                        module_id=module_id,
                        data_category="business",
                        resource_type="chunk",
                        resource_id=f"chunk-{chunk_id}",
                    )
                    prepared_chunk = {
                        **chunk,
                        "chunk_id": chunk_id,
                        "resource_id": chunk_identity.resource_id,
                        "resource_label": chunk_identity.label,
                        "module_id": module_id,
                    }
                    prepared.append(prepared_chunk)
                    points.append(
                        {
                            "id": self._point_id(chunk_id),
                            "vector": vector,
                            "payload": {
                                **prepared_chunk,
                                "document_id": document_id,
                                "source": document["source"],
                                "locator_id": locator_id,
                                "title": document["title"],
                                "shared_knowledge_base": True,
                                "embedding_model": embedding_model,
                                **chunk_identity.as_tags(),
                                "document_resource_id": identity.resource_id,
                                "document_resource_label": identity.label,
                                "classification": "private",
                                "version": 1,
                                "content_hash": digest,
                            },
                        }
                    )
                self.vector_store.replace_document(
                    document_id, points, module_id=module_id
                )
                self.repository.replace_document(
                    document={
                        "document_id": document_id,
                        "source": document["source"],
                        "title": document["title"],
                        "sha256": digest,
                        "character_count": len(document["text"]),
                        "embedding_model": embedding_model,
                        "module_id": module_id,
                        **identity.as_tags(),
                        "locator_id": locator_id,
                        "classification": "private",
                        "version": 1,
                    },
                    chunks=prepared,
                )
                indexed.append(
                    {
                        "document_id": document_id,
                        "source": document["source"],
                        "title": document["title"],
                        "character_count": len(document["text"]),
                        "chunk_count": len(prepared),
                        "module_id": module_id,
                        "resource_id": identity.resource_id,
                        "resource_label": identity.label,
                    }
                )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            result = self._dependency_error(exc, indexed=indexed)
            result["errors"] = errors
            return result
        return {
            "ok": True,
            "knowledge_base": "shared",
            "platform_id": PLATFORM_ID,
            "module_id": module_id,
            "collection": self.vector_store.COLLECTION,
            "indexed": indexed,
            "indexed_count": len(indexed),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "errors": errors,
            "embedding_model": embedding_model,
            "retrieval": "local-vector-degraded-cache+local-sqlite3-fts",
            "available_to_all_local_models": True,
            "network_used": False,
        }

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
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

    def _reconcile_source_document(
        self, module_id: str, locator_id: str
    ) -> dict[str, Any] | None:
        """A374: re-fetch the owning module's original content for replay.

        Prefers re-reading the document at its recorded source path (the
        true original); falls back to reassembled degraded-mirror chunk
        text.  Never returns degraded vectors — the reconciler re-chunks
        and re-embeds through the governed local runtime.
        """
        doc = self.repository.document_source(module_id, locator_id)
        if doc is None:
            return None
        source = str(doc.get("source") or "")
        if source:
            path = Path(source)
            if path.is_file():
                try:
                    doc["content"] = path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    pass  # keep reassembled mirror content
        return doc

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

    def _canonical_index_document(
        self,
        document_record: dict[str, Any],
        prepared: list[dict[str, Any]],
        points: list[dict[str, Any]],
        canonical_ready: bool,
    ) -> bool:
        """A373 write path: prove Qdrant + PostgreSQL are the live stores."""
        if not canonical_ready:
            return False
        canonical_chunks = [
            {
                **chunk,
                "point_id": point["id"],
                "qdrant_point_id": point["id"],
                "payload": point["payload"],
            }
            for chunk, point in zip(prepared, points)
        ]
        try:
            return bool(
                self.canonical.index_document(
                    document=document_record,
                    chunks=canonical_chunks,
                    vectors=[point["vector"] for point in points],
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self.canonical.mark_unhealthy(str(exc))
            return False

    def _ingest_document(
        self,
        document: dict[str, Any],
        module_id: str,
        embedding_model: str,
        canonical_ready: bool,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
        document_id = hashlib.sha256(document["source"].encode("utf-8")).hexdigest()[:32]
        unchanged = self._unchanged_document(
            document, document_id, module_id, embedding_model, digest,
            canonical_ready,
        )
        if unchanged is not None:
            return unchanged
        chunks = self._chunks(document["text"])
        vectors = self._embed([chunk["content"] for chunk in chunks])
        if not vectors or not vectors[0]:
            raise RuntimeError("RAG_EMBEDDING_EMPTY")
        self.vector_store.ensure_collection(len(vectors[0]))
        identity = ResourceIdentity(
            module_id=module_id,
            data_category="business",
            resource_type="document",
            resource_id=f"doc-{document_id}",
        )
        prepared, points = self._prepare_chunks(
            document, chunks, vectors, document_id, identity, module_id,
            embedding_model, digest,
        )
        document_record = self._document_record(
            document, document_id, identity, module_id, embedding_model,
            digest, len(prepared),
        )
        canonical_indexed = self._canonical_index_document(
            document_record, prepared, points, canonical_ready
        )
        # Local stores remain the bounded degraded mirror (A44); they are
        # written even on the canonical path so fallback reads stay consistent.
        self.vector_store.replace_document(document_id, points, module_id=module_id)
        self.repository.replace_document(document=document_record, chunks=prepared)
        if not canonical_indexed:
            # A374: record the durable pending_rag_mutation so canonical
            # recovery replays this write (re-fetch → re-chunk → re-embed).
            self._record_degraded_mutation(document_record)
        return self._ingest_entry(
            document, document_id, identity, module_id,
            len(prepared), canonical_indexed,
        )

    @staticmethod
    def _ingest_entry(
        document: dict[str, Any],
        document_id: str,
        identity: ResourceIdentity,
        module_id: str,
        chunk_count: int,
        canonical_indexed: bool,
    ) -> dict[str, Any]:
        return {
            "document_id": document_id,
            "source": document["source"],
            "title": document["title"],
            "character_count": len(document["text"]),
            "chunk_count": chunk_count,
            "module_id": module_id,
            "canonical": canonical_indexed,
            "reconciliation_required": not canonical_indexed,
            **identity.as_tags(),
        }

    def _unchanged_document(
        self,
        document: dict[str, Any],
        document_id: str,
        module_id: str,
        embedding_model: str,
        digest: str,
        canonical_ready: bool,
    ) -> dict[str, Any] | None:
        existing = self._existing_document(
            document["source"], document_id, module_id, canonical_ready
        )
        if existing and existing.get("sha256") == digest and existing.get("embedding_model") == embedding_model:
            return {
                "document_id": existing["document_id"],
                "source": document["source"],
                "reason": "unchanged",
            }
        return None

    def _record_degraded_mutation(self, document_record: dict[str, Any]) -> None:
        recorder = getattr(self.canonical, "record_degraded_mutation", None)
        if recorder is None:
            return
        try:
            recorder(document_record)
        except (OSError, RuntimeError, ValueError):
            pass  # enqueue is best-effort; the mirror write already landed

    @staticmethod
    def _document_record(
        document: dict[str, Any],
        document_id: str,
        identity: ResourceIdentity,
        module_id: str,
        embedding_model: str,
        digest: str,
        chunk_count: int,
    ) -> dict[str, Any]:
        return {
            "document_id": document_id,
            "source": document["source"],
            "title": document["title"],
            "sha256": digest,
            "character_count": len(document["text"]),
            "chunk_count": chunk_count,
            "embedding_model": embedding_model,
            "module_id": module_id,
            **identity.as_tags(),
            "locator_id": str(locator_id_for(module_id, identity.resource_id)),
            "classification": "private",
            "version": 1,
        }

    def _existing_document(
        self,
        source: str,
        document_id: str,
        module_id: str,
        canonical_ready: bool,
    ) -> dict[str, Any] | None:
        if canonical_ready:
            return self.canonical.fetch_document(
                module_id=module_id, resource_id=f"doc-{document_id}"
            )
        return self.repository.existing_document(source, module_id=module_id)

    def _prepare_chunks(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        document_id: str,
        identity: ResourceIdentity,
        module_id: str,
        embedding_model: str,
        digest: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        locator_id = str(locator_id_for(module_id, identity.resource_id))
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
                "source": document["source"],
                "title": document["title"],
            }
            prepared.append(prepared_chunk)
            points.append(
                {
                    "id": self._point_id(chunk_id),
                    "vector": vector,
                    "payload": {
                        **prepared_chunk,
                        "document_id": document_id,
                        "locator_id": locator_id,
                        "shared_knowledge_base": True,
                        "embedding_model": embedding_model,
                        **chunk_identity.as_tags(),
                        **self._document_tags(identity, digest),
                    },
                }
            )
        return prepared, points

    @staticmethod
    def _document_tags(identity: ResourceIdentity, digest: str) -> dict[str, Any]:
        return {
            "document_resource_id": identity.resource_id,
            "document_resource_label": identity.label,
            "classification": "private",
            "version": 1,
            "content_hash": digest,
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
        canonical_ready = self.canonical is not None and self.canonical.is_ready()
        try:
            for document in documents:
                entry = self._ingest_document(
                    document, module_id, embedding_model, canonical_ready
                )
                if entry.get("reason") == "unchanged":
                    skipped.append(entry)
                else:
                    indexed.append(entry)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            result = self._dependency_error(exc, indexed=indexed)
            result["errors"] = errors
            return result
        return self._ingest_result(
            module_id, embedding_model, canonical_ready,
            indexed, skipped, errors,
        )

    def _ingest_result(
        self,
        module_id: str,
        embedding_model: str,
        canonical_ready: bool,
        indexed: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
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
            "retrieval": (
                "canonical-qdrant-dense+postgresql-fts+index-state"
                if canonical_ready
                else "local-vector-degraded-cache+local-sqlite3-fts"
            ),
            "canonical": canonical_ready,
            "available_to_all_local_models": True,
            "network_used": False,
        }

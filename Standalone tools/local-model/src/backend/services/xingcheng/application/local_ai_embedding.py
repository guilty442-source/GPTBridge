from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np

from .investment_analysis import analyze_investments


class LocalAiEmbeddingMixin:
    def _infer_rag_context(self, prompt: str) -> dict[str, Any] | None:
        """Bounded local-RAG grounding for inference.

        Cheap gate first: when the local corpus is empty and canonical is
        not already warm, skip retrieval entirely so inference never pays
        embed/rerank latency for a guaranteed-empty result. Any failure
        returns None — retrieval must never block or break inference.
        """
        question = str(prompt or "").strip()
        if not question:
            return None
        canonical_ready = (
            self.local_rag.canonical is not None
            and self.local_rag.canonical.peek_ready()
        )
        try:
            document_count = int(
                (self.local_rag.repository.status() or {}).get(
                    "document_count"
                )
                or 0
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            document_count = 0
        if document_count <= 0 and not canonical_ready:
            return None
        context = self.local_rag.infer_context(question)
        if not isinstance(context, dict):
            return None
        citations = [
            {
                "citation_id": str(item.get("citation_id") or ""),
                "title": str(item.get("title") or "")[:200],
                "source": str(item.get("source") or "")[:200],
                "excerpt": str(item.get("excerpt") or "")[:360],
            }
            for item in (context.get("citations") or [])[:4]
            if isinstance(item, dict) and str(item.get("excerpt") or "").strip()
        ]
        if not citations:
            return None
        return {
            "citations": citations,
            "retrieval": str(context.get("retrieval") or ""),
            "canonical": context.get("canonical") is True,
            "authority": str(context.get("authority") or ""),
            "role": (
                "canonical-qdrant-postgresql"
                if context.get("canonical") is True
                else "bounded-degraded-cache"
            ),
        }

    def _embedding_retrieval(
        self, payload: dict[str, Any], prompt: str
    ) -> list[dict[str, Any]]:
        documents = payload.get("documents")
        if not isinstance(documents, list):
            return []
        candidates = [
            {
                "id": str(item.get("id") or f"document-{index + 1}")[:160],
                "content": str(item.get("content") or item.get("text") or "").strip()[:8_000],
            }
            for index, item in enumerate(documents[:32])
            if isinstance(item, dict)
            and str(item.get("content") or item.get("text") or "").strip()
        ]
        if not candidates:
            return []
        try:
            vectors = self.native_runtime.embed(
                [prompt, *(item["content"] for item in candidates)]
            )
        except (OSError, ValueError, RuntimeError):
            return []
        if len(vectors) != len(candidates) + 1:
            return []
        query = np.array(vectors[0], dtype=np.float32)
        query_norm = np.linalg.norm(query) or 1.0
        ranked: list[tuple[float, dict[str, Any]]] = []
        for item, vector in zip(candidates, vectors[1:]):
            vec = np.array(vector, dtype=np.float32)
            score = float(np.dot(query, vec) / (query_norm * (np.linalg.norm(vec) or 1.0)))
            ranked.append((score, item))
        ranked.sort(key=lambda row: row[0], reverse=True)
        return [
            {
                "id": item["id"],
                "content": item["content"][:2_000],
                "retrieval_score": round(score, 6),
                "model": self.native_runtime.EMBEDDING_MODEL,
            }
            for score, item in ranked[:6]
        ]

    def _prepare_runtime_output(
        self,
        payload: dict[str, Any],
        prompt: str,
        intent: str,
    ) -> dict[str, Any]:
        semantic = dict(payload.get("_semantic_plan") or {})
        embedding_retrieval = (
            []
            if str(payload.get("runtime_model") or "").strip()
            else self._embedding_retrieval(payload, prompt)
        )
        analysis = (
            analyze_investments(payload)
            if intent in {"analysis", "risk"}
            else None
        )
        market_research = (
            self.market_data.search(payload)
            if intent in {"search", "distribution", "quote"}
            else None
        )
        return {
            "ok": True,
            "intent": intent,
            "semantic_understanding": semantic,
            "response": "",
            "generation": {
                "text": "",
                "facts_preserved": True,
                "platform_preparation_only": True,
            },
            "analysis": analysis,
            "market_research": market_research,
            "fault_diagnostics": payload.get("fault_diagnostics"),
            "rag_context": payload.get("rag_context"),
            "evidence": embedding_retrieval,
            "embedding_retrieval": {
                "enabled": bool(embedding_retrieval),
                "model": self.native_runtime.EMBEDDING_MODEL,
                "result_count": len(embedding_retrieval),
            },
            "context_retrieval": {
                "memory_count": 0,
                "used_memory_count": 0,
                "memory_grounding_applied": False,
                "memory_ids": [],
                "reviewed_memory_only": True,
                "native_private_database_opened": False,
                "native_private_record_counts": {},
            },
            "evidence_policy": dict(self.native_model.runtime._EVIDENCE_POLICY),
            "instruction_execution": {
                "understood": True,
                "intent": intent,
                "governance_checked": True,
                "status": "planned",
            },
            "external_model_used": False,
            "third_party_weights_used": False,
            "star_native_model_used": False,
        }

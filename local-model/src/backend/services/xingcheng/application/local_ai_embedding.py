from __future__ import annotations

from typing import Any

import numpy as np

from .investment_analysis import analyze_investments


class LocalAiEmbeddingMixin:
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
            vectors = self.transformer_runtime.embed(
                [prompt, *(item["content"] for item in candidates)]
            )
        except (OSError, ValueError, RuntimeError):
            return []
        if len(vectors) != len(candidates) + 1:
            return []
        self.ollama_repositories[
            self.transformer_runtime.EMBEDDING_MODEL
        ].record_inference(
            intent="embedding-search",
            model_role="multilingual-project-retrieval",
            request={"query": prompt, "document_count": len(candidates)},
            response={
                "ok": True,
                "model": self.transformer_runtime.EMBEDDING_MODEL,
                "vector_count": len(vectors),
            },
        )
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
                "model": self.transformer_runtime.EMBEDDING_MODEL,
            }
            for score, item in ranked[:6]
        ]

    def _prepare_ollama_output(
        self,
        payload: dict[str, Any],
        prompt: str,
        intent: str,
    ) -> dict[str, Any]:
        semantic = dict(payload.get("_semantic_plan") or {})
        embedding_retrieval = self._embedding_retrieval(payload, prompt)
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
            "evidence": embedding_retrieval,
            "embedding_retrieval": {
                "enabled": bool(embedding_retrieval),
                "model": self.transformer_runtime.EMBEDDING_MODEL,
                "result_count": len(embedding_retrieval),
            },
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

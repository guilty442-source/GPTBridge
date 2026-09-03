from __future__ import annotations

import os
from typing import Any


class QwenReranker:
    """Lazy, local-cache-only adapter for Qwen3 CrossEncoder rerankers."""

    MODEL = "Qwen/Qwen3-Reranker-0.6B"

    def __init__(self) -> None:
        self._models: dict[str, Any] = {}
        self._errors: dict[str, str] = {}

    def _model_name(self, size: str) -> str:
        return str(os.environ.get("GPTBRIDGE_RAG_RERANKER_06B") or self.MODEL)

    def _load(self, size: str) -> Any:
        if size in self._models:
            return self._models[size]
        try:
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(
                self._model_name(size),
                local_files_only=True,
                trust_remote_code=False,
            )
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self._errors[size] = str(exc)[:500]
            return None
        self._models[size] = model
        self._errors.pop(size, None)
        return model

    def rerank(
        self, query: str, candidates: list[dict[str, Any]], *, size: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        normalized_size = "0.6b"
        model = self._load(normalized_size)
        if model is None:
            return candidates, {
                "applied": False,
                "model": self._model_name(normalized_size),
                "reason": self._errors.get(normalized_size, "local-reranker-unavailable"),
                "fallback": "rrf-hybrid-ranking",
            }
        passages = [str(item.get("content") or "") for item in candidates]
        try:
            scores = model.predict([(query, passage) for passage in passages])
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._errors[normalized_size] = str(exc)[:500]
            return candidates, {
                "applied": False,
                "model": self._model_name(normalized_size),
                "reason": self._errors[normalized_size],
                "fallback": "rrf-hybrid-ranking",
            }
        ranked = [
            {**item, "reranker_score": float(score)}
            for item, score in zip(candidates, scores)
        ]
        ranked.sort(key=lambda item: -float(item["reranker_score"]))
        return ranked, {
            "applied": True,
            "model": self._model_name(normalized_size),
            "candidate_count": len(ranked),
        }

    def status(self) -> dict[str, Any]:
        return {
            "model": self.MODEL,
            "loaded": sorted(self._models),
            "errors": dict(self._errors),
            "local_files_only": True,
        }


__all__ = ["QwenReranker"]

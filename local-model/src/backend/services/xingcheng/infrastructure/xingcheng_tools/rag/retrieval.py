"""Unified RAG Retrieval Interface — 本地 RAG 與 Web Search 共用介面。

對應需求 16, 17：
  統一流程：Query → Source Router → SQL / Local RAG / Web Search
    → Retriever → Reranker → Context Builder → 星澄
  網路搜尋與本地 RAG 必須共用統一的 Retrieval Interface。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable

from ..source import SourceMetadata, SourceType
from ..chunking import TextChunk


@dataclass
class RetrievalRequest:
    """統一檢索請求。"""

    query: str
    max_results: int = 10
    token_budget: int = 2048
    sources: list[SourceType] = field(
        default_factory=lambda: [SourceType.LOCAL_RAG, SourceType.WEB_SEARCH]
    )
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    """統一檢索結果。"""

    query: str
    chunks: list[TextChunk] = field(default_factory=list)
    metadata: list[SourceMetadata] = field(default_factory=list)
    source_breakdown: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "chunk_count": len(self.chunks),
            "source_breakdown": self.source_breakdown,
            "errors": self.errors,
        }


class Retriever(abc.ABC):
    """檢索器抽象介面。"""

    source_type: SourceType = SourceType.LOCAL_RAG

    @abc.abstractmethod
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        ...


class SourceRouter:
    """來源路由器 — 決定使用哪些來源。"""

    def __init__(self, routing_rules: dict[SourceType, bool] | None = None) -> None:
        self.routing_rules = routing_rules or {
            SourceType.LOCAL_RAG: True,
            SourceType.SQL: True,
            SourceType.WEB_SEARCH: False,  # 預設不自動啟用網路搜尋
            SourceType.MODEL_KNOWLEDGE: True,
        }

    def route(self, request: RetrievalRequest) -> list[SourceType]:
        """回傳應啟用的來源列表。"""
        if request.sources:
            return [s for s in request.sources if self.routing_rules.get(s, False)]
        return [s for s, enabled in self.routing_rules.items() if enabled]

    def enable(self, source: SourceType) -> None:
        self.routing_rules[source] = True

    def disable(self, source: SourceType) -> None:
        self.routing_rules[source] = False


class UnifiedRetriever:
    """統一檢索器 — 整合多個 Retriever。"""

    def __init__(
        self,
        retrievers: list[Retriever] | None = None,
        router: SourceRouter | None = None,
    ) -> None:
        self.retrievers: dict[SourceType, Retriever] = {}
        if retrievers:
            for r in retrievers:
                self.register(r)
        self.router = router or SourceRouter()

    def register(self, retriever: Retriever) -> None:
        self.retrievers[retriever.source_type] = retriever

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """從多個來源檢索並合併結果。"""
        sources = self.router.route(request)
        all_chunks: list[TextChunk] = []
        all_metadata: list[SourceMetadata] = []
        breakdown: dict[str, int] = {}
        errors: list[str] = []

        for source in sources:
            retriever = self.retrievers.get(source)
            if retriever is None:
                errors.append(f"未註冊 {source.value} retriever")
                continue
            try:
                result = retriever.retrieve(request)
                all_chunks.extend(result.chunks)
                all_metadata.extend(result.metadata)
                breakdown[source.value] = len(result.chunks)
                errors.extend(result.errors)
            except Exception as exc:
                errors.append(f"{source.value} 檢索失敗: {exc}")

        return RetrievalResult(
            query=request.query,
            chunks=all_chunks,
            metadata=all_metadata,
            source_breakdown=breakdown,
            errors=errors,
        )


__all__ = [
    "RetrievalRequest",
    "RetrievalResult",
    "Retriever",
    "SourceRouter",
    "UnifiedRetriever",
]

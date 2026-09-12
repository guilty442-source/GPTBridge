"""Source Metadata — 所有知識來源的統一元資料結構。

對應需求：來源追蹤。所有網路資料必須保存 Source Metadata，
星澄回答涉及網路搜尋結果時，要能回溯資訊來源。

來源類型至少區分：
  - Local RAG
  - SQL
  - Web Search
  - Model Knowledge
"""

from __future__ import annotations

import enum
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any


class SourceType(enum.Enum):
    """知識來源類型。"""

    LOCAL_RAG = "local_rag"
    SQL = "sql"
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    MODEL_KNOWLEDGE = "model_knowledge"
    GIT = "git"
    FILE = "file"
    SHELL = "shell"
    SYSTEM_RULE = "system_rule"


@dataclass
class SourceMetadata:
    """單一知識片段的完整來源元資料。

    網路來源必須包含 URL / Domain / Title / Published / Fetched /
    Search Query / Provider / Relevance Score。
    本地來源至少包含 source_type + source_id + retrieved_time。
    """

    source_type: SourceType
    url: str | None = None
    domain: str | None = None
    title: str | None = None
    author: str | None = None
    published_time: str | None = None
    updated_time: str | None = None
    fetched_time: float = field(default_factory=time.time)
    search_query: str | None = None
    provider: str | None = None
    relevance_score: float = 0.0
    source_id: str | None = None
    chunk_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.domain is None and self.url:
            self.domain = _extract_domain(self.url)
        if self.source_id is None:
            self.source_id = self._compute_id()

    def _compute_id(self) -> str:
        parts = [self.source_type.value, self.url or "", self.title or "", str(self.fetched_time)]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "url": self.url,
            "domain": self.domain,
            "title": self.title,
            "author": self.author,
            "published_time": self.published_time,
            "updated_time": self.updated_time,
            "fetched_time": self.fetched_time,
            "search_query": self.search_query,
            "provider": self.provider,
            "relevance_score": self.relevance_score,
            "source_id": self.source_id,
            "chunk_id": self.chunk_id,
            "extra": self.extra,
        }

    @classmethod
    def for_web(
        cls,
        url: str,
        title: str | None = None,
        *,
        search_query: str | None = None,
        provider: str | None = None,
        published_time: str | None = None,
        relevance_score: float = 0.0,
    ) -> "SourceMetadata":
        return cls(
            source_type=SourceType.WEB_SEARCH,
            url=url,
            title=title,
            search_query=search_query,
            provider=provider,
            published_time=published_time,
            relevance_score=relevance_score,
        )

    @classmethod
    def for_local_rag(
        cls,
        source_id: str,
        title: str | None = None,
        relevance_score: float = 0.0,
    ) -> "SourceMetadata":
        return cls(
            source_type=SourceType.LOCAL_RAG,
            source_id=source_id,
            title=title,
            relevance_score=relevance_score,
        )

    @classmethod
    def for_sql(cls, query: str, relevance_score: float = 0.0) -> "SourceMetadata":
        return cls(
            source_type=SourceType.SQL,
            source_id=hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
            extra={"sql_query": query},
            relevance_score=relevance_score,
        )

    @classmethod
    def for_model_knowledge(cls) -> "SourceMetadata":
        return cls(source_type=SourceType.MODEL_KNOWLEDGE)


def _extract_domain(url: str) -> str:
    """從 URL 萃取域名。"""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc or parsed.hostname or ""
    except Exception:
        return ""


__all__ = ["SourceMetadata", "SourceType"]

"""搜尋層共用型別：SearchRequest / SearchStatus / SearchResult / FetchStatus。

對應需求 22-25, 28, 38-39：
  - Search Request 統一格式
  - Search Result 標準化
  - Fetch Status
  - Search Status
  - 搜尋工具回傳格式
"""

from __future__ import annotations

import enum
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ── Search Status (需求 39) ─────────────────────────────────────
class SearchStatus(enum.Enum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    NO_RESULT = "no_result"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    FETCH_ERROR = "fetch_error"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


# ── Fetch Status (需求 28) ──────────────────────────────────────
class FetchStatus(enum.Enum):
    PENDING = "pending"
    FETCHING = "fetching"
    SUCCESS = "success"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    INVALID = "invalid"
    TOO_LARGE = "too_large"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


# ── 搜尋優先級 ──────────────────────────────────────────────────
class SearchPriority(enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


# ── Search Request (需求 22) ────────────────────────────────────
@dataclass
class SearchRequest:
    """統一搜尋請求格式。各 Search Provider 必須遵守此介面。"""

    original_question: str
    queries: list[str] = field(default_factory=list)
    language: str = "zh-TW"
    time_range: str | None = None          # 例如 "past24h", "pastweek", "pastmonth"
    source_restrictions: list[str] = field(default_factory=list)  # 指定網站
    domain_blocklist: list[str] = field(default_factory=list)
    max_results: int = 10
    need_full_text: bool = True
    need_reranker: bool = True
    need_citations: bool = True
    priority: SearchPriority = SearchPriority.NORMAL
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_time: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "original_question": self.original_question,
            "queries": self.queries,
            "language": self.language,
            "time_range": self.time_range,
            "source_restrictions": self.source_restrictions,
            "domain_blocklist": self.domain_blocklist,
            "max_results": self.max_results,
            "need_full_text": self.need_full_text,
            "need_reranker": self.need_reranker,
            "need_citations": self.need_citations,
            "priority": self.priority.value,
            "created_time": self.created_time,
            "extra": self.extra,
        }


# ── Search Result (需求 25) ─────────────────────────────────────
@dataclass
class SearchResult:
    """統一搜尋結果格式。所有 Provider 回傳後必須轉成此格式。"""

    result_id: str
    title: str
    url: str
    domain: str
    snippet: str = ""
    published_time: str | None = None
    updated_time: str | None = None
    rank: int = 0
    provider: str = ""
    search_query: str = ""
    fetch_status: FetchStatus = FetchStatus.PENDING
    relevance_score: float = 0.0
    source_type: str = "web_search"
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.result_id:
            self.result_id = hashlib.sha256(
                f"{self.url}|{self.search_query}".encode("utf-8")
            ).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "title": self.title,
            "url": self.url,
            "domain": self.domain,
            "snippet": self.snippet,
            "published_time": self.published_time,
            "updated_time": self.updated_time,
            "rank": self.rank,
            "provider": self.provider,
            "search_query": self.search_query,
            "fetch_status": self.fetch_status.value,
            "relevance_score": self.relevance_score,
            "source_type": self.source_type,
        }


# ── 搜尋工具回傳格式 (需求 38) ──────────────────────────────────
@dataclass
class SearchResponse:
    """搜尋工具內部回傳給 Tool Router 的統一格式。"""

    request_id: str
    status: SearchStatus
    queries: list[str] = field(default_factory=list)
    result_count: int = 0
    fetched_count: int = 0
    valid_source_count: int = 0
    evidence_count: int = 0
    selected_sources: list[dict[str, Any]] = field(default_factory=list)
    evidence_text: str = ""
    source_metadata: list[dict[str, Any]] = field(default_factory=list)
    execution_time: float = 0.0
    errors: list[str] = field(default_factory=list)
    cache_hits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status.value,
            "queries": self.queries,
            "result_count": self.result_count,
            "fetched_count": self.fetched_count,
            "valid_source_count": self.valid_source_count,
            "evidence_count": self.evidence_count,
            "selected_sources": self.selected_sources,
            "evidence_text": self.evidence_text,
            "source_metadata": self.source_metadata,
            "execution_time": self.execution_time,
            "errors": self.errors,
            "cache_hits": self.cache_hits,
        }


__all__ = [
    "SearchStatus",
    "FetchStatus",
    "SearchPriority",
    "SearchRequest",
    "SearchResult",
    "SearchResponse",
]

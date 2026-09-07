"""SearXNG Provider — 主要搜尋聚合入口。

對應需求：
  - SearXNG 作為主要搜尋聚合入口
  - 優先允許本機部署
  - 不將星澄模型核心綁定特定搜尋供應商

SearXNG 是一個開源 meta 搜尋引擎，可本機部署，
聚合 Google / Bing / DuckDuckGo / Wikipedia 等多個來源。
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

from .provider import SearchProvider, SearchProviderError
from .types import SearchRequest, SearchResult
from ..fetch.safety import URLSafetyChecker, URLSafetyError

log = logging.getLogger(__name__)
NETWORK_DESTINATION_ALLOWLIST = frozenset({"127.0.0.1", "localhost", "::1"})


class SearXNGProvider(SearchProvider):
    """SearXNG 搜尋 Provider。"""

    name = "searxng"
    supports_time_range = True
    supports_site_search = True

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        safety: URLSafetyChecker | None = None,
        timeout: float = 15.0,
        max_results_per_query: int = 10,
    ) -> None:
        normalized_url = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(normalized_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in NETWORK_DESTINATION_ALLOWLIST:
            raise SearchProviderError("SearXNG endpoint must use the governed loopback allowlist")
        self.base_url = normalized_url
        self.timeout = max(1.0, min(30.0, float(timeout)))
        self.max_results_per_query = max_results_per_query
        self.safety = safety or URLSafetyChecker()

    def is_available(self) -> bool:
        return self.safety.is_safe(self.base_url, allow_local=True)

    def search(self, request: SearchRequest) -> list[SearchResult]:
        """向 SearXNG 發送搜尋請求。"""
        all_results: list[SearchResult] = []
        for query in request.queries:
            try:
                results = self._search_single(query, request)
                all_results.extend(results)
            except Exception as exc:
                log.warning("SearXNG query '%s' failed: %s", query, exc)
        return all_results

    def _search_single(self, query: str, request: SearchRequest) -> list[SearchResult]:
        params: dict[str, Any] = {
            "q": query,
            "format": "json",
            "safesearch": 1,
            "language": request.language,
        }
        if request.time_range and self.supports_time_range:
            params["time_range"] = request.time_range
        if request.source_restrictions and self.supports_site_search:
            # SearXNG 支援 sites 查詢參數
            params["sites"] = ",".join(request.source_restrictions)

        url = f"{self.base_url}/search?{urllib.parse.urlencode(params)}"

        try:
            safe_url = self.safety.validate(url, allow_local=True)
        except URLSafetyError as exc:
            raise SearchProviderError(f"SearXNG URL 安全檢查失敗: {exc}") from exc

        import urllib.request
        req = urllib.request.Request(safe_url, headers={
            "User-Agent": "XingCheng/1.0",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            raise SearchProviderError(f"SearXNG 請求失敗: {exc}") from exc

        return self._normalize_results(data, query, request)

    def _normalize_results(
        self,
        data: dict[str, Any],
        query: str,
        request: SearchRequest,
    ) -> list[SearchResult]:
        """將 SearXNG 原始回應轉成統一 SearchResult。"""
        raw_results = data.get("results", [])
        results: list[SearchResult] = []
        for rank, item in enumerate(raw_results[: self.max_results_per_query]):
            url = item.get("url", "")
            if not url:
                continue
            results.append(
                SearchResult(
                    result_id="",  # 自動生成
                    title=item.get("title", ""),
                    url=url,
                    domain=self._extract_domain(url),
                    snippet=item.get("content", ""),
                    published_time=item.get("publishedDate"),
                    rank=rank,
                    provider=self.name,
                    search_query=query,
                    relevance_score=max(0.0, 1.0 - rank * 0.1),
                    source_type="web_search",
                    raw=item,
                )
            )
        return results

    @staticmethod
    def _extract_domain(url: str) -> str:
        try:
            parsed = urllib.parse.urlparse(url)
            return parsed.netloc or ""
        except Exception:
            return ""


__all__ = ["SearXNGProvider"]

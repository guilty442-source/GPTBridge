"""Searchd Provider — Go 原生 metasearch 服務適配器。

對應需求：
  - go-service 層僅透過版本化合約溝通（``xingcheng-searchd/v1``，
    契約定義見 ``Standalone tools/searchd-go/CONTRACT.md``）
  - 取代 searchd 外部依賴；治理邊界不變（loopback allowlist +
    URLSafetyChecker + metadata-only 結果）
  - Provider 錯誤不得造成星澄整體失敗（上層 ``_run_web_search``
    仍負責 fail-closed 降級）
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any

from .provider import SearchProvider, SearchProviderError
from .types import SearchRequest, SearchResult
from ..fetch.safety import URLSafetyChecker, URLSafetyError

log = logging.getLogger(__name__)
NETWORK_DESTINATION_ALLOWLIST = frozenset({"127.0.0.1", "localhost", "::1"})
CONTRACT = "xingcheng-searchd/v1"
DEFAULT_URL = "http://127.0.0.1:8091"


class SearchdProvider(SearchProvider):
    """Go searchd 搜尋 Provider（loopback ``/v1/search`` JSON 契約）。"""

    name = "searchd"
    supports_time_range = True
    supports_site_search = True

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        *,
        safety: URLSafetyChecker | None = None,
        timeout: float = 15.0,
        max_results_per_query: int = 10,
    ) -> None:
        normalized_url = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(normalized_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in NETWORK_DESTINATION_ALLOWLIST:
            raise SearchProviderError("searchd endpoint must use the governed loopback allowlist")
        self.base_url = normalized_url
        self.timeout = max(1.0, min(30.0, float(timeout)))
        self.max_results_per_query = max_results_per_query
        self.safety = safety or URLSafetyChecker()
        # hostname 已被 loopback allowlist 驗證；把自訂 port 註冊進
        # checker 的授權本地端點（預設 8091 已在靜態清單）。
        port = parsed.port
        endpoint_key = (
            f"{parsed.hostname}:{port}" if port else str(parsed.hostname)
        )
        self.safety.config.allowed_local_endpoints.add(endpoint_key)
        self.last_adapter_status: list[dict[str, Any]] = []

    def is_available(self) -> bool:
        if not self.safety.is_safe(self.base_url, allow_local=True):
            return False
        try:
            req = urllib.request.Request(
                f"{self.base_url}/healthz",
                headers={"User-Agent": "XingCheng/1.0"},
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return data.get("ok") is True
        except Exception:
            return False

    def search(self, request: SearchRequest) -> list[SearchResult]:
        all_results: list[SearchResult] = []
        for query in request.queries:
            try:
                all_results.extend(self._search_single(query, request))
            except Exception as exc:
                log.warning("searchd query '%s' failed: %s", query, exc)
        return all_results

    def _search_single(self, query: str, request: SearchRequest) -> list[SearchResult]:
        url = f"{self.base_url}/v1/search"
        try:
            safe_url = self.safety.validate(url, allow_local=True)
        except URLSafetyError as exc:
            raise SearchProviderError(f"searchd URL 安全檢查失敗: {exc}") from exc

        body: dict[str, Any] = {
            "request_id": request.request_id,
            "query": query,
            "language": request.language,
            "max_results": request.max_results,
            "safesearch": True,
        }
        if request.time_range:
            body["time_range"] = request.time_range
        if request.source_restrictions:
            body["sites"] = list(request.source_restrictions)

        req = urllib.request.Request(
            safe_url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "User-Agent": "XingCheng/1.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            raise SearchProviderError(f"searchd 請求失敗: {exc}") from exc

        if not data.get("ok"):
            raise SearchProviderError(f"searchd 回傳錯誤: {data.get('error')}")
        self.last_adapter_status = list(data.get("adapters") or [])
        return self._normalize_results(data, query, request)

    def _normalize_results(
        self,
        data: dict[str, Any],
        query: str,
        request: SearchRequest,
    ) -> list[SearchResult]:
        raw_results = data.get("results", [])
        results: list[SearchResult] = []
        for rank, item in enumerate(raw_results[: self.max_results_per_query]):
            url = item.get("url", "")
            if not url:
                continue
            results.append(
                SearchResult(
                    result_id="",
                    title=item.get("title", ""),
                    url=url,
                    domain=item.get("domain") or self._extract_domain(url),
                    snippet=item.get("snippet", ""),
                    published_time=item.get("published"),
                    rank=rank,
                    provider=item.get("engine") or self.name,
                    search_query=query,
                    relevance_score=float(item.get("score") or 0.0),
                    source_type="web_search",
                    raw={k: item[k] for k in ("engine", "score") if k in item},
                )
            )
        return results

    @staticmethod
    def _extract_domain(url: str) -> str:
        try:
            return urllib.parse.urlsplit(url).hostname or ""
        except Exception:
            return ""


__all__ = ["SearchdProvider"]

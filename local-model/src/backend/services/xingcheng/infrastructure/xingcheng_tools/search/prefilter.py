"""Pre-Filter — 搜尋結果初篩。

對應需求 26：
  在全文抓取前先做低成本過濾：
  重複 URL、無效 URL、黑名單 Domain、低品質內容、明顯廣告頁、
  登入牆、無正文頁面、不支援格式、與 Query 明顯無關結果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .types import SearchResult
from ..fetch.safety import URLSafetyChecker


# 低品質 / 廣告 / 登入牆 domain 模式
_LOW_QUALITY_DOMAIN_PATTERNS = (
    re.compile(r"^(www\.)?(ads|ad|sponsor|promo)\.", re.IGNORECASE),
    re.compile(r"\.(ads|ad|sponsor)\.", re.IGNORECASE),
)

# 登入牆 URL 模式
_LOGIN_WALL_PATTERNS = (
    re.compile(r"/login", re.IGNORECASE),
    re.compile(r"/signin", re.IGNORECASE),
    re.compile(r"/auth", re.IGNORECASE),
    re.compile(r"/account/login", re.IGNORECASE),
)

# 無正文 / 不支援格式 URL
_NO_CONTENT_PATTERNS = (
    re.compile(r"\.(pdf|zip|tar|gz|rar|7z|exe|dmg|apk|iso|img)$", re.IGNORECASE),
    re.compile(r"\.(jpg|jpeg|png|gif|svg|webp|bmp|ico|mp4|mp3|avi|mov)$", re.IGNORECASE),
)

# 廣告 / 推廣 snippet 模式
_AD_SNIPPET_PATTERNS = (
    re.compile(r"(?:buy|shop|purchase|order|廣告|贊助|sponsored|ad:)", re.IGNORECASE),
)


@dataclass
class PreFilterConfig:
    """初篩設定。"""

    domain_blocklist: set[str] = field(default_factory=set)
    domain_allowlist: set[str] = field(default_factory=set)
    min_snippet_length: int = 10
    min_title_length: int = 3
    max_results: int = 20
    drop_ads: bool = True
    drop_login_walls: bool = True
    drop_binary_files: bool = True


class PreFilter:
    """搜尋結果初篩器。"""

    def __init__(
        self,
        config: PreFilterConfig | None = None,
        safety: URLSafetyChecker | None = None,
    ) -> None:
        self.config = config or PreFilterConfig()
        self.safety = safety or URLSafetyChecker()

    def filter(
        self,
        results: list[SearchResult],
        queries: list[str] | None = None,
    ) -> tuple[list[SearchResult], list[str]]:
        """過濾搜尋結果。回傳 (filtered, dropped_reasons)。"""
        dropped: list[str] = []
        seen_urls: set[str] = set()
        seen_domains: dict[str, int] = {}
        filtered: list[SearchResult] = []

        for result in results:
            reason = self._check_single(result, seen_urls, seen_domains, queries)
            if reason:
                dropped.append(f"{result.url}: {reason}")
                continue
            seen_urls.add(result.url)
            seen_domains[result.domain] = seen_domains.get(result.domain, 0) + 1
            filtered.append(result)
            if len(filtered) >= self.config.max_results:
                break

        return filtered, dropped

    def _check_single(
        self,
        result: SearchResult,
        seen_urls: set[str],
        seen_domains: dict[str, int],
        queries: list[str] | None,
    ) -> str | None:
        # URL 去重
        if result.url in seen_urls:
            return "重複 URL"
        # URL 安全
        if not self.safety.is_safe(result.url):
            return "無效或不安全 URL"
        # Domain 黑名單
        if result.domain in self.config.domain_blocklist:
            return f"黑名單 domain: {result.domain}"
        # Domain 白名單
        if self.config.domain_allowlist and result.domain not in self.config.domain_allowlist:
            return f"不在白名單: {result.domain}"
        # 低品質 domain
        if any(p.match(result.domain) for p in _LOW_QUALITY_DOMAIN_PATTERNS):
            return "低品質/廣告 domain"
        # 同一 domain 最多 3 筆（避免單一來源壟斷）
        if seen_domains.get(result.domain, 0) >= 3:
            return f"同 domain 結果過多: {result.domain}"
        # 登入牆
        if self.config.drop_login_walls and any(p.search(result.url) for p in _LOGIN_WALL_PATTERNS):
            return "可能登入牆"
        # 不支援格式
        if self.config.drop_binary_files and any(p.search(result.url) for p in _NO_CONTENT_PATTERNS):
            return "不支援的檔案格式"
        # 標題過短
        if len(result.title.strip()) < self.config.min_title_length:
            return "標題過短"
        # 廣告 snippet
        if self.config.drop_ads and any(p.search(result.snippet) for p in _AD_SNIPPET_PATTERNS):
            return "廣告內容"
        # 與 Query 明顯無關（簡易檢查：標題和 snippet 都不包含任何 query 詞）
        if queries:
            combined = (result.title + " " + result.snippet).lower()
            if not any(q.lower() in combined for q in queries if len(q) > 2):
                return "與 Query 明顯無關"
        return None


__all__ = ["PreFilter", "PreFilterConfig"]

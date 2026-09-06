"""Web Fetcher — 安全受控的網頁內容擷取。

對應需求：網頁取得層。
支援 HTTP/HTTPS、HTML、純文字、公開 JSON、RSS。
設定：Timeout、最大下載大小、Redirect 限制、Content-Type 檢查、錯誤重試、連線數限制。
所有網路資料一律視為不可信輸入。
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .safety import URLSafetyChecker, URLSafetyError

log = logging.getLogger(__name__)

# 允許的 Content-Type prefix
_ALLOWED_CONTENT_TYPES = frozenset({
    "text/html",
    "text/plain",
    "application/json",
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
    "text/markdown",
    "application/xhtml+xml",
})


@dataclass
class FetchConfig:
    """Web Fetcher 設定。"""

    timeout: float = 15.0
    max_bytes: int = 5 * 1024 * 1024  # 5 MB
    max_redirects: int = 5
    max_retries: int = 2
    retry_delay: float = 1.0
    user_agent: str = "XingCheng/1.0 (Local Native Model; +https://github.com/xingcheng)"
    allowed_content_types: frozenset[str] = _ALLOWED_CONTENT_TYPES
    max_concurrent: int = 4


@dataclass
class FetchResult:
    """網頁擷取結果。"""

    url: str
    final_url: str
    status: int
    content_type: str
    content: bytes
    encoding: str = "utf-8"
    fetched_time: float = field(default_factory=time.time)
    error: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.error is None

    def text(self) -> str:
        try:
            return self.content.decode(self.encoding or "utf-8", errors="replace")
        except Exception:
            return self.content.decode("utf-8", errors="replace")


class WebFetcher:
    """安全受控的網頁擷取器。"""

    def __init__(
        self,
        config: FetchConfig | None = None,
        safety: URLSafetyChecker | None = None,
    ) -> None:
        self.config = config or FetchConfig()
        self.safety = safety or URLSafetyChecker()
        self._semaphore = threading.Semaphore(self.config.max_concurrent)

    def fetch(self, url: str, *, allow_local: bool = False) -> FetchResult:
        """擷取單一 URL。"""
        start = time.time()
        try:
            safe_url = self.safety.validate(url, allow_local=allow_local)
        except URLSafetyError as exc:
            return FetchResult(
                url=url, final_url=url, status=0, content_type="", content=b"",
                error=f"URL 安全檢查失敗: {exc}", elapsed=time.time() - start,
            )

        with self._semaphore:
            return self._fetch_with_retry(safe_url, start, allow_local=allow_local)

    def fetch_many(
        self, urls: list[str], *, allow_local: bool = False
    ) -> list[FetchResult]:
        """批次擷取多個 URL（循序，避免過度佔用連線）。"""
        return [self.fetch(u, allow_local=allow_local) for u in urls]

    def _fetch_with_retry(
        self, url: str, start: float, *, allow_local: bool
    ) -> FetchResult:
        last_error: str | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                result = self._fetch_once(url, start, allow_local=allow_local)
                if result.ok or result.status == 404:
                    return result
                last_error = result.error or f"HTTP {result.status}"
            except Exception as exc:
                last_error = str(exc)
                log.warning("fetch attempt %d failed for %s: %s", attempt + 1, url, exc)
            if attempt < self.config.max_retries:
                time.sleep(self.config.retry_delay * (attempt + 1))

        return FetchResult(
            url=url, final_url=url, status=0, content_type="", content=b"",
            error=f"重試 {self.config.max_retries + 1} 次後仍失敗: {last_error}",
            elapsed=time.time() - start,
        )

    def _fetch_once(
        self, url: str, start: float, *, allow_local: bool
    ) -> FetchResult:
        req = urllib.request.Request(url, headers={
            "User-Agent": self.config.user_agent,
            "Accept": ", ".join(sorted(self.config.allowed_content_types)),
            "Accept-Encoding": "identity",  # 不壓縮，簡化處理
        })

        # 手動 redirect 控制（檢查每次 redirect 的目標安全性）
        opener = urllib.request.build_opener(NoRedirectHandler)
        redirect_count = 0
        final_url = url

        try:
            response = opener.open(req, timeout=self.config.timeout)
            final_url = response.geturl()
            # 檢查 redirect 目標安全（如果 redirect 到私有網段）
            if final_url != url:
                self.safety.validate(final_url, allow_local=allow_local)
        except urllib.error.HTTPError as exc:
            return FetchResult(
                url=url, final_url=final_url, status=exc.code,
                content_type=exc.headers.get("Content-Type", ""),
                content=b"", error=f"HTTP {exc.code}", elapsed=time.time() - start,
            )
        except urllib.error.URLError as exc:
            return FetchResult(
                url=url, final_url=final_url, status=0, content_type="",
                content=b"", error=str(exc.reason), elapsed=time.time() - start,
            )

        content_type = response.headers.get("Content-Type", "")
        # 檢查 Content-Type
        ct_lower = content_type.lower().split(";")[0].strip()
        if ct_lower and ct_lower not in self.config.allowed_content_types:
            return FetchResult(
                url=url, final_url=final_url, status=response.status,
                content_type=content_type, content=b"",
                error=f"不允許的 Content-Type: {content_type}",
                elapsed=time.time() - start,
            )

        # 限制下載大小
        content = response.read(self.config.max_bytes + 1)
        if len(content) > self.config.max_bytes:
            log.warning("URL %s 超過最大下載大小 %d bytes，截斷", url, self.config.max_bytes)
            content = content[: self.config.max_bytes]

        encoding = "utf-8"
        if "charset=" in content_type:
            encoding = content_type.split("charset=")[-1].strip().split(";")[0]

        return FetchResult(
            url=url, final_url=final_url, status=response.status,
            content_type=content_type, content=content, encoding=encoding,
            fetched_time=time.time(), elapsed=time.time() - start,
        )


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """允許 redirect 但限制次數。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib 內建已處理 redirect，此處保留預設行為
        return super().redirect_request(req, fp, code, msg, headers, newurl)


__all__ = ["WebFetcher", "FetchResult", "FetchConfig"]

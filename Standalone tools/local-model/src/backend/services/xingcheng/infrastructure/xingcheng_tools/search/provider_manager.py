"""Provider Manager — 搜尋供應商管理與 fallback。

對應需求 24：
  Provider A → 成功 → 使用結果
  Provider A → Timeout / Error → 重試 → 仍失敗 → Fallback Provider → 繼續執行
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .provider import SearchProvider, SearchProviderError, SearchProviderTimeout
from .types import SearchRequest, SearchResult

log = logging.getLogger(__name__)


class ProviderManager:
    """管理多個 Search Provider，提供 fallback 機制。"""

    def __init__(
        self,
        providers: list[SearchProvider] | None = None,
        *,
        max_retries: int = 1,
        retry_delay: float = 1.0,
    ) -> None:
        self.providers: list[SearchProvider] = providers or []
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def add_provider(self, provider: SearchProvider) -> None:
        self.providers.append(provider)

    def remove_provider(self, name: str) -> None:
        self.providers = [p for p in self.providers if p.name != name]

    def get_provider(self, name: str) -> SearchProvider | None:
        for p in self.providers:
            if p.name == name:
                return p
        return None

    def search(self, request: SearchRequest) -> tuple[list[SearchResult], list[str]]:
        """依序嘗試 Provider，成功即回傳；失敗則 fallback。

        回傳 (results, errors)。
        """
        errors: list[str] = []
        for provider in self.providers:
            if not provider.is_available():
                errors.append(f"{provider.name}: 不可用")
                continue
            results = self._search_with_retry(provider, request, errors)
            if results:
                log.info("Provider %s 回傳 %d 筆結果", provider.name, len(results))
                return results, errors
        return [], errors

    def _search_with_retry(
        self,
        provider: SearchProvider,
        request: SearchRequest,
        errors: list[str],
    ) -> list[SearchResult]:
        for attempt in range(self.max_retries + 1):
            try:
                start = time.time()
                results = provider.search(request)
                elapsed = time.time() - start
                log.debug("Provider %s 搜尋耗時 %.2fs", provider.name, elapsed)
                return results
            except SearchProviderTimeout as exc:
                msg = f"{provider.name} timeout (attempt {attempt + 1}): {exc}"
                errors.append(msg)
                log.warning(msg)
            except SearchProviderError as exc:
                msg = f"{provider.name} error (attempt {attempt + 1}): {exc}"
                errors.append(msg)
                log.warning(msg)
            except Exception as exc:
                msg = f"{provider.name} 未知錯誤 (attempt {attempt + 1}): {exc}"
                errors.append(msg)
                log.warning(msg)
            if attempt < self.max_retries:
                time.sleep(self.retry_delay * (attempt + 1))
        return []

    def health_check(self) -> list[dict[str, Any]]:
        return [p.health_check() for p in self.providers]


__all__ = ["ProviderManager"]

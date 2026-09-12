"""Search Provider Interface — 統一搜尋供應商介面。

對應需求 3, 24：
  - 建立統一 Search Provider Interface
  - 上層不得依賴單一搜尋引擎 API 格式
  - Provider 發生錯誤時不得直接造成星澄整體失敗
"""

from __future__ import annotations

import abc
import logging
from typing import Any

from .types import SearchRequest, SearchResult

log = logging.getLogger(__name__)


class SearchProvider(abc.ABC):
    """搜尋供應商抽象介面。所有 Provider 必須實作此介面。"""

    name: str = "abstract"
    supports_time_range: bool = False
    supports_site_search: bool = False

    @abc.abstractmethod
    def search(self, request: SearchRequest) -> list[SearchResult]:
        """執行搜尋，回傳標準化 SearchResult 列表。"""
        ...

    def is_available(self) -> bool:
        """檢查 Provider 是否可用。"""
        return True

    def health_check(self) -> dict[str, Any]:
        """健康檢查。"""
        return {"name": self.name, "available": self.is_available()}


class SearchProviderError(Exception):
    """Provider 執行錯誤。"""


class SearchProviderTimeout(SearchProviderError):
    """Provider 逾時。"""


__all__ = ["SearchProvider", "SearchProviderError", "SearchProviderTimeout"]

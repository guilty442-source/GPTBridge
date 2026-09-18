"""星澄受管網路介面卡 (governed network adapters)。

與 `native_transformer/`（模型核心）嚴格分離。本套件只保留治理稽核
釘定的網路邊界宣告：loopback-only 的 SearXNG provider 與 URL 安全檢查。
外部故障排除研究走 `ai-collaboration` 受管通道，不經此套件。
"""

from __future__ import annotations

from .search.types import (
    SearchRequest, SearchResponse, SearchStatus,
    SearchResult, FetchStatus, SearchPriority,
)
from .search.provider import SearchProvider, SearchProviderError
from .search.searxng import SearXNGProvider
from .fetch.safety import URLSafetyChecker, URLSafetyError, URLSafetyConfig

__version__ = "2.00000"

__all__ = [
    "SearchRequest",
    "SearchResponse",
    "SearchStatus",
    "SearchResult",
    "FetchStatus",
    "SearchPriority",
    "SearchProvider",
    "SearchProviderError",
    "SearXNGProvider",
    "URLSafetyChecker",
    "URLSafetyError",
    "URLSafetyConfig",
]

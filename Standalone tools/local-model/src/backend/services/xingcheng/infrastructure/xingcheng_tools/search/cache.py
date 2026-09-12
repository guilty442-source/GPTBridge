"""Web Cache — 依資料型態設定不同 TTL。

對應需求 13, 43：
  - 相同 URL 在合理時間內不要重複下載
  - 依資料類型設定不同 TTL
  - Cache 命中流程：Cache Lookup → Freshness Check → 可用/過期
"""

from __future__ import annotations

import enum
import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any


class CacheTier(enum.Enum):
    """Cache TTL 分級。"""

    REALTIME = ("realtime", 60)         # 即時資訊：60 秒
    NEWS = ("news", 900)                # 新聞：15 分鐘
    DOCUMENTATION = ("documentation", 7200)  # 文件：2 小時
    STATIC = ("static", 86400)          # 靜態知識頁：24 小時
    SEARCH_RESULT = ("search_result", 300)   # 搜尋結果：5 分鐘


@dataclass
class CacheEntry:
    """Cache 項目。"""

    key: str
    value: Any
    tier: CacheTier
    created_time: float = field(default_factory=time.time)
    ttl: float = 0.0
    hit_count: int = 0

    def __post_init__(self) -> None:
        if self.ttl <= 0:
            self.ttl = self.tier.value[1]

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_time) > self.ttl

    @property
    def remaining_ttl(self) -> float:
        return max(0.0, self.ttl - (time.time() - self.created_time))


class WebCache:
    """Thread-safe Web Cache。"""

    def __init__(self, max_entries: int = 1000) -> None:
        self._cache: dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self.max_entries = max_entries

    @staticmethod
    def make_key(url: str, *, prefix: str = "fetch") -> str:
        return f"{prefix}:{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expired:
                del self._cache[key]
                return None
            entry.hit_count += 1
            return entry

    def put(self, key: str, value: Any, tier: CacheTier = CacheTier.STATIC) -> CacheEntry:
        with self._lock:
            if len(self._cache) >= self.max_entries:
                self._evict_oldest()
            entry = CacheEntry(key=key, value=value, tier=tier)
            self._cache[key] = entry
            return entry

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._cache),
                "max_entries": self.max_entries,
                "total_hits": sum(e.hit_count for e in self._cache.values()),
            }

    def _evict_oldest(self) -> None:
        if not self._cache:
            return
        oldest_key = min(self._cache, key=lambda k: self._cache[k].created_time)
        del self._cache[oldest_key]

    # ── 便利方法 ────────────────────────────────────────────────
    def get_or_put(
        self,
        key: str,
        factory: Any,
        tier: CacheTier = CacheTier.STATIC,
    ) -> tuple[Any, bool]:
        """若 Cache 有效則回傳，否則呼叫 factory 並存入。回傳 (value, cache_hit)。"""
        entry = self.get(key)
        if entry is not None:
            return entry.value, True
        value = factory() if callable(factory) else factory
        self.put(key, value, tier)
        return value, False

    def guess_tier(self, url: str, content_type: str = "") -> CacheTier:
        """依 URL / Content-Type 猜測 TTL 分級。"""
        lower_url = url.lower()
        if any(kw in lower_url for kw in ("news", "breaking", "headline", "即時")):
            return CacheTier.NEWS
        if any(kw in lower_url for kw in ("price", "quote", "stock", "行情", "報價")):
            return CacheTier.REALTIME
        if any(kw in lower_url for kw in ("docs.", "documentation", "/docs/", "/api/")):
            return CacheTier.DOCUMENTATION
        return CacheTier.STATIC


__all__ = ["WebCache", "CacheEntry", "CacheTier"]

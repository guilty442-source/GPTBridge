"""Locator Cache (E12).

LRU cache for resource_id → locator mappings.  TTL 30-300 seconds.
Only caches opaque locator and metadata — never bypasses governance
or caches unrestricted physical paths.

Usage:
    from shared_layer.database.locator_cache import LocatorCache

    cache = LocatorCache(max_size=10000, ttl_seconds=120)
    locator = cache.get(resource_id)
    if locator is None:
        locator = fetch_from_db(resource_id)
        cache.put(resource_id, locator)

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist; deny-by-default.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional


class LocatorCache:
    """Thread-safe LRU cache with TTL for opaque locator lookups.

    Only stores opaque locator + metadata.  Never stores unrestricted
    physical paths or bypasses governance checks.
    """

    def __init__(
        self,
        *,
        max_size: int = 10000,
        ttl_seconds: int = 120,
    ) -> None:
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[dict[str, Any], float]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, resource_id: str) -> Optional[dict[str, Any]]:
        """Get a cached locator.  Returns None if missing or expired."""
        with self._lock:
            entry = self._cache.get(resource_id)
            if entry is None:
                self._misses += 1
                return None
            value, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._cache[resource_id]
                self._misses += 1
                return None
            # Move to end (most recently used)
            self._cache.move_to_end(resource_id)
            self._hits += 1
            return dict(value)

    def put(
        self,
        resource_id: str,
        locator: dict[str, Any],
    ) -> None:
        """Cache a locator.  Only opaque locator + metadata allowed."""
        # Safety: never cache unrestricted physical paths
        if "unrestricted_path" in locator or "raw_path" in locator:
            return
        with self._lock:
            now = time.monotonic()
            expires_at = now + self._ttl_seconds
            if resource_id in self._cache:
                self._cache.move_to_end(resource_id)
            self._cache[resource_id] = (dict(locator), expires_at)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
                self._evictions += 1

    def invalidate(self, resource_id: str) -> None:
        """Invalidate a single entry."""
        with self._lock:
            self._cache.pop(resource_id, None)

    def clear(self) -> None:
        """Clear the entire cache."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def stats(self) -> dict[str, int]:
        """Get cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "ttl_seconds": self._ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "hit_rate": (self._hits / total) if total > 0 else 0,
            }


# Default singleton instance
_default_cache: Optional[LocatorCache] = None
_default_lock = threading.Lock()


def get_default_cache() -> LocatorCache:
    """Get the default singleton LocatorCache."""
    global _default_cache
    if _default_cache is None:
        with _default_lock:
            if _default_cache is None:
                _default_cache = LocatorCache()
    return _default_cache


__all__ = ["LocatorCache", "get_default_cache"]

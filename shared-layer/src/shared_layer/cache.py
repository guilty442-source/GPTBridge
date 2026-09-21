"""Shared Layer Cache - High-performance in-memory caching with TTL and LRU eviction.

This module provides:
- LRUCache: Thread-safe LRU cache with TTL support
- AsyncCache: Async-compatible cache with background refresh
- CacheKey: Standardized cache key generation
- CacheMetrics: Performance metrics collection
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    """Cache entry with value, timestamp, and TTL."""
    value: T
    created_at: float = field(default_factory=time.monotonic)
    ttl: float = 300.0  # 5 minutes default

    def is_expired(self) -> bool:
        return time.monotonic() - self.created_at > self.ttl


class LRUCache(Generic[T]):
    """Thread-safe LRU cache with TTL support and size limit.

    W5: internally sharded by key hash (16 shards, each with its own lock)
    so concurrent readers/writers contend only within a shard. Eviction is
    per-shard LRU — capacity is ``ceil(max_size / shards)`` per shard.
    """

    _SHARD_COUNT = 16

    def __init__(self, max_size: int = 1000, default_ttl: float = 300.0) -> None:
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._shard_max = max(1, -(-max_size // self._SHARD_COUNT))
        self._caches: list[OrderedDict[str, CacheEntry[T]]] = [
            OrderedDict() for _ in range(self._SHARD_COUNT)
        ]
        self._locks = [threading.RLock() for _ in range(self._SHARD_COUNT)]

    def _shard(self, key: str) -> int:
        return hash(key) % self._SHARD_COUNT

    def get(self, key: str) -> Optional[T]:
        """Get value from cache, return None if not found or expired."""
        shard = self._shard(key)
        with self._locks[shard]:
            cache = self._caches[shard]
            entry = cache.get(key)
            if entry is None:
                return None
            if entry.is_expired():
                cache.pop(key, None)
                return None
            # Move to end (most recently used)
            cache.move_to_end(key)
            return entry.value

    def set(self, key: str, value: T, ttl: Optional[float] = None) -> None:
        """Set value in cache with optional TTL."""
        shard = self._shard(key)
        with self._locks[shard]:
            cache = self._caches[shard]
            if key in cache:
                cache.move_to_end(key)
            elif len(cache) >= self._shard_max:
                cache.popitem(last=False)
            cache[key] = CacheEntry(
                value=value,
                ttl=ttl or self._default_ttl
            )

    def delete(self, key: str) -> bool:
        """Delete key from cache."""
        shard = self._shard(key)
        with self._locks[shard]:
            return self._caches[shard].pop(key, None) is not None

    def clear(self) -> None:
        """Clear all cache entries."""
        for lock, cache in zip(self._locks, self._caches):
            with lock:
                cache.clear()

    def size(self) -> int:
        """Return current cache size."""
        total = 0
        for lock, cache in zip(self._locks, self._caches):
            with lock:
                total += len(cache)
        return total


class AsyncCache(Generic[T]):
    """Async-compatible cache with background refresh support."""

    def __init__(self, max_size: int = 1000, default_ttl: float = 300.0) -> None:
        self._lru_cache = LRUCache[T](max_size, default_ttl)
        self._refresh_tasks: dict[str, asyncio.Task] = {}
        self._refresh_lock = asyncio.Lock()

    async def get(self, key: str, loader: Callable[[], T], ttl: Optional[float] = None) -> T:
        """Get value from cache or load using loader function."""
        value = self._lru_cache.get(key)
        if value is not None:
            return value
        # Load value
        value = await asyncio.to_thread(loader)
        self._lru_cache.set(key, value, ttl)
        return value

    async def get_or_refresh(
        self,
        key: str,
        loader: Callable[[], T],
        ttl: Optional[float] = None,
        force_refresh: bool = False,
    ) -> T:
        """Get value, optionally forcing a refresh in background."""
        if not force_refresh:
            value = self._lru_cache.get(key)
            if value is not None:
                return value
        # Force refresh
        value = await asyncio.to_thread(loader)
        self._lru_cache.set(key, value, ttl)
        return value

    def invalidate(self, key: str) -> bool:
        """Invalidate a cache key."""
        return self._lru_cache.delete(key)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._lru_cache.clear()


def cache_key(prefix: str, *parts: Any) -> str:
    """Generate a standardized cache key."""
    return f"{prefix}:{':'.join(str(p) for p in parts)}"


def cached(ttl: float = 300.0, max_size: int = 1000):
    """Decorator for caching function results with LRU cache."""
    cache = LRUCache[Any](max_size=max_size, default_ttl=ttl)

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            key = cache_key(func.__name__, args, tuple(sorted(kwargs.items())))
            value = cache.get(key)
            if value is not None:
                return value
            value = func(*args, **kwargs)
            cache.set(key, value, ttl)
            return value
        return wrapper
    return decorator
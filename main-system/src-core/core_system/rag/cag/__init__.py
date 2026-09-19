"""CAG cache package — A549 cache-augmented generation plane (L1-L3)."""

from __future__ import annotations

from .contracts import (
    CACHE_AUTHORITIES,
    IMPLEMENTED_CACHE_LEVELS,
    CacheAuthority,
    CacheEntry,
    CacheGateDecision,
    CacheInvalidationReason,
    CacheKey,
    CacheLevel,
    CacheRequest,
    normalize_query,
)
from .gate import CagGate
from .store import DEFAULT_CAPS, DEFAULT_TTLS, CagCacheStore

__all__ = [
    "CACHE_AUTHORITIES",
    "DEFAULT_CAPS",
    "DEFAULT_TTLS",
    "IMPLEMENTED_CACHE_LEVELS",
    "CacheAuthority",
    "CacheEntry",
    "CacheGateDecision",
    "CacheInvalidationReason",
    "CacheKey",
    "CacheLevel",
    "CacheRequest",
    "CagCacheStore",
    "CagGate",
    "normalize_query",
]

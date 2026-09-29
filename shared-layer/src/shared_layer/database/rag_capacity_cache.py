"""RAG caches: embedding (stable, long TTL) vs retrieval (short-lived)."""
from __future__ import annotations

import threading
import time
from typing import Any, Optional, Sequence

from .rag_capacity_helpers import embedding_cache_key, retrieval_cache_key


# ============================================================================
# Two cache kinds — embedding (stable) vs retrieval (short-lived)
# ============================================================================

class _TTLCache:
    def __init__(self, ttl_seconds: int, max_size: int = 4096) -> None:
        self._ttl = ttl_seconds
        self._max = max_size
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._cache[key]
                self._misses += 1
                return None
            self._hits += 1
            return value

    def _put(self, key: str, value: Any) -> None:
        with self._lock:
            if key not in self._cache and len(self._cache) >= self._max:
                seq = sorted(self._cache.items(), key=lambda kv: kv[1][1])
                self._cache.pop(seq[0][0], None)
            self._cache[key] = (value, time.monotonic() + self._ttl)

    def _drop(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "max_size": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (self._hits / total) if total > 0 else 0,
            }


class EmbeddingCache(_TTLCache):
    """Embedding cache: text_hash + model + dimension.  Very stable, long TTL.

    Stores vector bytes only — never content or paths.
    """

    def __init__(self, ttl_seconds: int = 86_400, max_size: int = 16384) -> None:
        super().__init__(ttl_seconds, max_size)

    def get(self, text_hash: str, model: str, dimension: int) -> Optional[tuple[float, ...]]:
        return self._get(embedding_cache_key(text_hash=text_hash, model=model, dimension=dimension))

    def put(self, text_hash: str, model: str, dimension: int, vector: Sequence[float]) -> None:
        self._put(
            embedding_cache_key(text_hash=text_hash, model=model, dimension=dimension),
            tuple(vector),
        )


class RetrievalCache(_TTLCache):
    """Retrieval/reranker result cache: query + scope + generation + policy.

    Short-lived; any generation change splits the key space so stale hits can
    never survive an index swap.  Cache retrieval candidates and reranker
    scores — NOT the final LLM answer.
    """

    def __init__(self, ttl_seconds: int = 60, max_size: int = 8192) -> None:
        super().__init__(ttl_seconds, max_size)

    def get(
        self,
        *,
        query: str,
        module_scope: Sequence[str],
        generation_id: str,
        policy_version: str,
    ) -> Optional[Any]:
        key = retrieval_cache_key(
            query=query,
            module_scope=module_scope,
            generation_id=generation_id,
            policy_version=policy_version,
        )
        return self._get(key)

    def put(
        self,
        *,
        query: str,
        module_scope: Sequence[str],
        generation_id: str,
        policy_version: str,
        value: Any,
    ) -> None:
        key = retrieval_cache_key(
            query=query,
            module_scope=module_scope,
            generation_id=generation_id,
            policy_version=policy_version,
        )
        self._put(key, value)

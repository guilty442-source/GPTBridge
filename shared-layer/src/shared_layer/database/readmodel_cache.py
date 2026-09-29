"""Read model caches: TTL + generation/revision-aware projection caches."""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

from .readmodel_core import CACHE_SIZE, CACHE_TTL_SECONDS


# ============================================================================
# AuthoritativeCache — TTL + generation + revision aware projection cache
# ============================================================================

class AuthoritativeCache:
    """Small TTL cache for projection reads with generation fencing.

    Entries are keyed by (projection, filter_value) and carry their registry
    revision, source generation and projection version.  A generation bump or
    TTL expiry invalidates without touching the database.  Caching an empty
    result set is the negative cache: a projection with no rows is served
    cheaply instead of re-scanning authority.

    Uses only the projection tables — it never caches unrestricted paths or
    any data that bypasses governance.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = CACHE_TTL_SECONDS,
        max_size: int = CACHE_SIZE,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_size = max_size
        self._cache: dict[tuple[str, Optional[str]], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._invalidations = 0

    def put(
        self,
        projection: str,
        data: list[dict[str, Any]],
        meta: dict[str, Any],
        *,
        filter_value: Optional[str] = None,
        source_revision: Optional[int] = None,
    ) -> None:
        key = (projection, filter_value)
        entry = {
            "data": list(data),
            "meta": dict(meta),
            "source_revision": int(source_revision) if source_revision is not None else int(meta.get("projection_revision") or 0),
            "expires_at": time.monotonic() + self._ttl_seconds,
        }
        with self._lock:
            if key not in self._cache and len(self._cache) >= self._max_size:
                self._evict_one()
            self._cache[key] = entry

    def get(
        self,
        projection: str,
        *,
        filter_value: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        key = (projection, filter_value)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            if time.monotonic() >= entry["expires_at"]:
                del self._cache[key]
                self._misses += 1
                return None
            self._hits += 1
            return {
                "data": list(entry["data"]),
                "meta": dict(entry["meta"]),
                "source_revision": entry["source_revision"],
            }

    def invalidate(self, projection: str) -> None:
        with self._lock:
            for key in [k for k in self._cache if k[0] == projection]:
                del self._cache[key]
                self._invalidations += 1

    def invalidate_all(self) -> None:
        with self._lock:
            self._invalidations += len(self._cache)
            self._cache.clear()

    def _evict_one(self) -> None:
        if not self._cache:
            return
        # oldest expiry first (approx LRU by insertion for equal TTL)
        seq = sorted(self._cache.items(), key=lambda kv: kv[1]["expires_at"])
        self._cache.pop(seq[0][0], None)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "ttl_seconds": self._ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "invalidations": self._invalidations,
                "hit_rate": (self._hits / total) if total > 0 else 0,
            }


# ============================================================================
# SecuritySnapshotCache — permission/security snapshot fast path (migration 021)
# ============================================================================

class SecuritySnapshotCache:
    """Cache for permission snapshots with security-generation fencing.

    Act as the *read-side* fast path for :mod:`shared_layer.security`
    decisions.  Snapshots are captured against a permission generation and a
    security generation; any generation bump (permission change, rotation,
    revocation) invalidates the whole cache because individual decisions are
    cheap to rebuild via the authority snapshot machinery.

    This cache never decides — it only serves what was already decided by the
    security layer.  On cache miss the caller re-captures a snapshot.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 60,
        max_size: int = 8192,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_size = max_size
        self._cache: dict[tuple[str, str, Optional[str], str], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._high_water_security_generation: int = 0
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(
        actor_id: str,
        target_module: str,
        target_resource_id: Optional[str],
        operation: str,
    ) -> tuple[str, str, Optional[str], str]:
        return (actor_id, target_module, target_resource_id, operation)

    def put(
        self,
        *,
        actor_id: str,
        target_module: str,
        target_resource_id: Optional[str],
        operation: str,
        snapshot: dict[str, Any],
        security_generation: Optional[int] = None,
        permission_generation: Optional[int] = None,
    ) -> None:
        key = self._key(actor_id, target_module, target_resource_id, operation)
        entry = {
            "snapshot": dict(snapshot),
            "permission_generation": permission_generation,
            "security_generation": security_generation,
            "expires_at": time.monotonic() + self._ttl_seconds,
        }
        with self._lock:
            if security_generation is not None and security_generation > self._high_water_security_generation:
                self._high_water_security_generation = security_generation
            if key not in self._cache and len(self._cache) >= self._max_size:
                seq = sorted(self._cache.items(), key=lambda kv: kv[1]["expires_at"])
                self._cache.pop(seq[0][0], None)
            self._cache[key] = entry

    def get(
        self,
        *,
        actor_id: str,
        target_module: str,
        target_resource_id: Optional[str],
        operation: str,
        security_generation: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        key = self._key(actor_id, target_module, target_resource_id, operation)
        high_water = self._high_water_security_generation
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            if time.monotonic() >= entry["expires_at"]:
                del self._cache[key]
                self._misses += 1
                return None
            sg = entry.get("security_generation")
            # Any bump recorded at or after this snapshot's generation makes it
            # unconditionally stale (revocation must take effect immediately).
            if high_water > (sg if sg is not None else 0):
                del self._cache[key]
                self._misses += 1
                return None
            if security_generation is not None and sg is not None and security_generation != sg:
                del self._cache[key]
                self._misses += 1
                return None
            self._hits += 1
            return {
                "snapshot": dict(entry["snapshot"]),
                "permission_generation": entry["permission_generation"],
                "security_generation": entry["security_generation"],
            }

    def invalidate(self) -> None:
        """Wholesale invalidation after any permission/security change."""
        with self._lock:
            self._cache.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "ttl_seconds": self._ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "high_water_security_generation": self._high_water_security_generation,
                "hit_rate": (self._hits / total) if total > 0 else 0,
            }

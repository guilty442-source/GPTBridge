"""CAG cache store — bounded L1-L3 caches behind the CAG Gate (A549).

Every lookup runs the gate; a denied or expired entry is never returned.
Stores are bounded per level (LRU eviction) and carry only cache/derived
authority payloads.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import replace
from typing import Any, Callable, Mapping

from .contracts import (
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


DEFAULT_TTLS: Mapping[CacheLevel, float] = {
    CacheLevel.L1: 60.0,
    CacheLevel.L2: 600.0,
    CacheLevel.L3: 3600.0,
    CacheLevel.L4: 3600.0,
}

DEFAULT_CAPS: Mapping[CacheLevel, int] = {
    CacheLevel.L1: 256,
    CacheLevel.L2: 1024,
    CacheLevel.L3: 4096,
    CacheLevel.L4: 4096,
}


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class CagCacheStore:
    """Bounded, gated cache store for the CAG plane."""

    def __init__(
        self,
        gate: CagGate | None = None,
        *,
        caps: Mapping[CacheLevel, int] | None = None,
        ttls: Mapping[CacheLevel, float] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._gate = gate or CagGate()
        self._caps = {**DEFAULT_CAPS, **(dict(caps) if caps else {})}
        self._ttls = {**DEFAULT_TTLS, **(dict(ttls) if ttls else {})}
        self._clock = clock or time.time
        self._entries: dict[CacheLevel, "OrderedDict[str, CacheEntry]"] = {
            level: OrderedDict() for level in CacheLevel
        }
        self._invalidations: list[dict[str, Any]] = []

    @staticmethod
    def key_for(request: CacheRequest) -> CacheKey:
        return CacheKey(
            tenant_id=request.tenant_id,
            module_id=request.module_ids[0] if request.module_ids else "",
            identity_id=request.identity_id,
            permission_scope=request.permission_scope,
            data_classification=request.data_classification,
            query_normalized=normalize_query(request.query),
            model_version=request.model_version,
            policy_version=request.policy_version,
            source_revision=request.source_revision,
            rag_architectures=tuple(sorted(request.rag_architectures)),
            generation_mode=request.generation_mode,
            active_generation=request.active_generation,
            embedding_model=request.embedding_model,
            embedding_dimension=request.embedding_dimension,
            reranker_version=request.reranker_version,
            chunk_policy_version=request.chunk_policy_version,
            context_builder_version=request.context_builder_version,
        )

    def put(
        self,
        request: CacheRequest,
        payload: Mapping[str, Any],
        *,
        generation_id: str = "",
        version: int = 1,
        authority: CacheAuthority = CacheAuthority.CACHE,
        evidence_ids: tuple[str, ...] = (),
        resource_ids: tuple[str, ...] = (),
        content_hashes: tuple[str, ...] = (),
        source_versions: tuple[str, ...] = (),
        context_hash: str = "",
        canonical_source: str = "",
    ) -> CacheEntry:
        now = self._clock()
        key = self.key_for(request)
        entry = CacheEntry(
            cache_id=f"cag-{key.digest()[:16]}-{request.level.value}",
            key=key,
            level=request.level,
            authority=authority,
            payload=dict(payload),
            payload_digest=_digest(payload),
            created_at=now,
            expires_at=now + float(self._ttls.get(request.level, 60.0)),
            version=max(1, int(version)),
            generation_id=generation_id,
            evidence_ids=evidence_ids,
            resource_ids=resource_ids,
            content_hashes=content_hashes,
            source_versions=source_versions,
            context_hash=context_hash,
            canonical_source=canonical_source,
            validated_at=now,
        )
        bucket = self._entries[request.level]
        digest = key.digest()
        bucket[digest] = entry
        bucket.move_to_end(digest)
        self._evict(request.level)
        return entry

    def get(self, request: CacheRequest) -> tuple[CacheEntry | None, CacheGateDecision]:
        key_digest = self.key_for(request).digest()
        bucket = self._entries[request.level]
        entry = bucket.get(key_digest)
        if entry is None:
            return None, CacheGateDecision(
                allowed=False, reason="cache-miss", checks={}
            )
        decision = self._gate.validate(entry, request, now=self._clock())
        if not decision.allowed:
            bucket.pop(key_digest, None)
            return None, decision
        bucket.move_to_end(key_digest)
        refreshed = replace(
            entry,
            hit_count=entry.hit_count + 1,
            last_hit_at=self._clock(),
        )
        bucket[key_digest] = refreshed
        return refreshed, decision

    def invalidate(
        self,
        request: CacheRequest,
        reason: CacheInvalidationReason | str = CacheInvalidationReason.MANUAL,
    ) -> bool:
        key_digest = self.key_for(request).digest()
        bucket = self._entries[request.level]
        entry = bucket.get(key_digest)
        if entry is None:
            return False
        bucket.pop(key_digest, None)
        self._record_invalidation(entry, reason)
        return True

    def invalidate_module(
        self,
        module_id: str,
        reason: CacheInvalidationReason | str = CacheInvalidationReason.MANUAL,
    ) -> int:
        removed = 0
        for bucket in self._entries.values():
            for key_digest in [k for k, e in bucket.items() if e.key.module_id == module_id]:
                entry = bucket.pop(key_digest, None)
                if entry is not None:
                    self._record_invalidation(entry, reason)
                    removed += 1
        return removed

    def invalidation_log(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._invalidations)

    def _record_invalidation(
        self, entry: CacheEntry, reason: CacheInvalidationReason | str
    ) -> None:
        self._invalidations.append(
            {
                "cache_id": entry.cache_id,
                "module_id": entry.key.module_id,
                "level": entry.level.value,
                "reason": str(
                    reason.value if isinstance(reason, CacheInvalidationReason) else reason
                ),
                "at": self._clock(),
            }
        )
        if len(self._invalidations) > 100:
            del self._invalidations[:-100]

    def purge_expired(self) -> int:
        now = self._clock()
        removed = 0
        for bucket in self._entries.values():
            for key_digest in [k for k, e in bucket.items() if e.is_expired(now)]:
                bucket.pop(key_digest, None)
                removed += 1
        return removed

    def stats(self) -> dict[str, Any]:
        now = self._clock()
        per_level: dict[str, dict[str, int]] = {}
        total = 0
        expired = 0
        for level, bucket in self._entries.items():
            level_expired = sum(1 for entry in bucket.values() if entry.is_expired(now))
            per_level[level.value] = {
                "entries": len(bucket),
                "expired": level_expired,
                "cap": int(self._caps.get(level, 0)),
            }
            total += len(bucket)
            expired += level_expired
        return {
            "levels": per_level,
            "total_entries": total,
            "expired_entries": expired,
            "implemented_levels": ["L1", "L2", "L3"],
        }

    def _evict(self, level: CacheLevel) -> None:
        cap = int(self._caps.get(level, 0))
        if cap <= 0:
            return
        bucket = self._entries[level]
        while len(bucket) > cap:
            bucket.popitem(last=False)


__all__ = ["CagCacheStore", "DEFAULT_CAPS", "DEFAULT_TTLS"]

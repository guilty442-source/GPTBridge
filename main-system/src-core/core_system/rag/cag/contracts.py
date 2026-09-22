"""CAG cache contracts — A549 cache-augmented generation plane.

A549 requires every cache to carry version, tenant, module, identity,
permission scope, data classification, query normalization, model and policy
version, source revision, created/expiry times, invalidation reason and an
authority marker, and requires every cache hit to pass the CAG Gate's scope,
permission, revision, expiry and authority validation.  Caches are derived or
cache authority only and never an official fact source.
"""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class CacheLevel(str, Enum):
    """Cache tiers.  L1-L3 are implemented; L4 needs explicit approval."""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class CacheAuthority(str, Enum):
    """Authority marker — a cache is never canonical."""

    DERIVED = "derived"
    CACHE = "cache"


class CacheInvalidationReason(str, Enum):
    """Registered invalidation reasons."""

    EXPIRED = "expired"
    REVISION_CHANGED = "revision-changed"
    POLICY_CHANGED = "policy-changed"
    MODEL_CHANGED = "model-changed"
    PERMISSION_CHANGED = "permission-changed"
    SOURCE_DELETED = "source-deleted"
    SOURCE_UPDATED = "source-updated"
    TOMBSTONED = "tombstoned"
    GENERATION_CHANGED = "generation-changed"
    CHUNK_POLICY_CHANGED = "chunk-policy-changed"
    RERANKER_CHANGED = "reranker-changed"
    CLASSIFICATION_CHANGED = "classification-changed"
    MANUAL = "manual"


IMPLEMENTED_CACHE_LEVELS: frozenset[CacheLevel] = frozenset(
    {CacheLevel.L1, CacheLevel.L2, CacheLevel.L3}
)
CACHE_AUTHORITIES: frozenset[CacheAuthority] = frozenset(
    {CacheAuthority.DERIVED, CacheAuthority.CACHE}
)


def normalize_query(query: str) -> str:
    """Canonical query normalization used as part of the cache key."""
    normalized = unicodedata.normalize("NFKC", str(query or ""))
    return " ".join(normalized.strip().casefold().split())


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Full cache identity: every A549 cache dimension is key material."""

    tenant_id: str
    module_id: str
    identity_id: str
    permission_scope: str
    data_classification: str
    query_normalized: str
    model_version: str
    policy_version: str
    source_revision: str
    rag_architectures: tuple[str, ...] = ("hybrid",)
    generation_mode: str = "canonical"
    active_generation: str = ""
    embedding_model: str = ""
    embedding_dimension: int = 0
    reranker_version: str = ""
    chunk_policy_version: str = ""
    context_builder_version: str = ""

    def digest(self) -> str:
        return _digest(self.to_record())

    def to_record(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "module_id": self.module_id,
            "identity_id": self.identity_id,
            "permission_scope": self.permission_scope,
            "data_classification": self.data_classification,
            "query_normalized": self.query_normalized,
            "model_version": self.model_version,
            "policy_version": self.policy_version,
            "source_revision": self.source_revision,
            "rag_architectures": list(self.rag_architectures),
            "generation_mode": self.generation_mode,
            "active_generation": self.active_generation,
            "embedding_model": self.embedding_model,
            "embedding_dimension": self.embedding_dimension,
            "reranker_version": self.reranker_version,
            "chunk_policy_version": self.chunk_policy_version,
            "context_builder_version": self.context_builder_version,
        }


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One stored cache entry with metadata and payload digest."""

    cache_id: str
    key: CacheKey
    level: CacheLevel
    authority: CacheAuthority
    payload: Mapping[str, Any]
    payload_digest: str
    created_at: float
    expires_at: float
    version: int = 1
    generation_id: str = ""
    invalidation_reason: str = ""
    evidence_ids: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()
    content_hashes: tuple[str, ...] = ()
    source_versions: tuple[str, ...] = ()
    context_hash: str = ""
    canonical_source: str = ""
    validated_at: float = 0.0
    hit_count: int = 0
    last_hit_at: float = 0.0
    state: str = "VALID"

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def to_record(self) -> dict[str, Any]:
        return {
            "cache_id": self.cache_id,
            "key": self.key.to_record(),
            "level": self.level.value,
            "authority": self.authority.value,
            "payload_digest": self.payload_digest,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "version": self.version,
            "generation_id": self.generation_id,
            "invalidation_reason": self.invalidation_reason,
            "evidence_ids": list(self.evidence_ids),
            "resource_ids": list(self.resource_ids),
            "content_hashes": list(self.content_hashes),
            "source_versions": list(self.source_versions),
            "context_hash": self.context_hash,
            "canonical_source": self.canonical_source,
            "validated_at": self.validated_at,
            "hit_count": self.hit_count,
            "last_hit_at": self.last_hit_at,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class CacheRequest:
    """A caller's cache lookup/store declaration."""

    tenant_id: str
    module_ids: tuple[str, ...]
    identity_id: str
    permission_scope: str
    data_classification: str
    query: str
    model_version: str
    policy_version: str
    source_revision: str
    level: CacheLevel = CacheLevel.L1
    level4_approved: bool = False
    rag_architectures: tuple[str, ...] = ("hybrid",)
    generation_mode: str = "canonical"
    active_generation: str = ""
    embedding_model: str = ""
    embedding_dimension: int = 0
    reranker_version: str = ""
    chunk_policy_version: str = ""
    context_builder_version: str = ""

    def normalized_query(self) -> str:
        return normalize_query(self.query)


@dataclass(frozen=True, slots=True)
class CacheGateDecision:
    """Fail-closed gate verdict for one cache hit."""

    allowed: bool
    reason: str
    checks: Mapping[str, bool] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "checks": dict(self.checks),
        }


__all__ = [
    "CACHE_AUTHORITIES",
    "IMPLEMENTED_CACHE_LEVELS",
    "CacheAuthority",
    "CacheEntry",
    "CacheGateDecision",
    "CacheInvalidationReason",
    "CacheKey",
    "CacheLevel",
    "CacheRequest",
    "normalize_query",
]

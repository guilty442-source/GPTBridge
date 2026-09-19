"""CAG Gate — mandatory validation for every cache hit (A549).

A cache hit is only usable after the gate verifies scope, permission,
revision, expiry and authority, plus the cache identity dimensions (tenant,
module, identity, data classification, model/policy version and normalized
query).  Any failed check denies the hit fail-closed.
"""

from __future__ import annotations

import time
from typing import Mapping

from .contracts import (
    CACHE_AUTHORITIES,
    IMPLEMENTED_CACHE_LEVELS,
    CacheEntry,
    CacheGateDecision,
    CacheLevel,
    CacheRequest,
)


class CagGate:
    """Validates cache entries against a request; never mutates state."""

    def __init__(self, *, level4_approved_modules: frozenset[str] = frozenset()) -> None:
        self._level4_approved_modules = frozenset(level4_approved_modules)

    def validate(
        self,
        entry: CacheEntry,
        request: CacheRequest,
        *,
        now: float | None = None,
    ) -> CacheGateDecision:
        checks: dict[str, bool] = {}

        checks["level-implemented"] = entry.level in IMPLEMENTED_CACHE_LEVELS or (
            entry.level is CacheLevel.L4
            and (
                request.level4_approved
                or entry.key.module_id in self._level4_approved_modules
            )
        )
        checks["level-match"] = entry.level is request.level
        checks["scope"] = entry.key.module_id in request.module_ids
        checks["permission"] = _scope_covers(
            request.permission_scope, entry.key.permission_scope
        )
        checks["tenant"] = entry.key.tenant_id == request.tenant_id
        checks["identity"] = entry.key.identity_id == request.identity_id
        checks["data-classification"] = (
            entry.key.data_classification == request.data_classification
        )
        checks["query-normalization"] = (
            entry.key.query_normalized == request.normalized_query()
        )
        checks["model-version"] = entry.key.model_version == request.model_version
        checks["policy-version"] = entry.key.policy_version == request.policy_version
        checks["revision"] = entry.key.source_revision == request.source_revision
        checks["expiry"] = not entry.is_expired(now if now is not None else time.time())
        checks["authority"] = entry.authority in CACHE_AUTHORITIES

        for check, passed in checks.items():
            if not passed:
                return CacheGateDecision(
                    allowed=False, reason=f"cache-denied:{check}", checks=checks
                )
        return CacheGateDecision(allowed=True, reason="cache-allowed", checks=checks)


def _scope_covers(request_scope: str, entry_scope: str) -> bool:
    """True when the request scope covers the entry's stored scope."""
    requested = str(request_scope or "").strip()
    stored = str(entry_scope or "").strip()
    if not requested or not stored:
        return False
    if requested == stored:
        return True
    if requested.endswith(":*"):
        return stored.startswith(requested[:-1])
    return False


__all__ = ["CagGate"]

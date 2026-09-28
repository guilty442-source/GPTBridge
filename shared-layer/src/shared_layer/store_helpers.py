"""Shared helpers for store.py and store_async.py (A185 split).

Kept here to avoid circular imports between store.py and store_async.py.
Serialization helpers (``now_iso``/``decode``/``encode_json``) live in
``store_codec`` per the source-size contract.
"""
from __future__ import annotations

import os
from typing import Final

from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
)

_CHANS: Final[frozenset[str]] = frozenset({"system", "ai"})
_MAX_ID: Final[int] = 256
_MAX_BYTES: Final[int] = 1_048_576
_QUERY_TIMEOUT: Final[float] = 10.0
# Pool ceiling aligned with AdaptiveEnvelope(pool 2-8): the static transport
# pool must never exceed the adaptive plane's tunable maximum.
_POOL_MIN_CONN: Final[int] = 2
_POOL_MAX_CONN: Final[int] = 8
_POOL_TIMEOUT: Final[float] = 5.0
# C59 POOL-ISOLATION: pooled connections idle beyond this TTL are closed so
# a quiet process does not pin backend slots it will never use again.
_POOL_IDLE_TTL_S: Final[float] = float(
    os.environ.get("GPTBRIDGE_STORE_POOL_IDLE_TTL_S", "120")
)


def normalize_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > _MAX_ID or "`x00" in normalized:
        raise permission_denied()
    return normalized


# Keep in sync with shared-layer/migrations/041_transport_priority_queue.sql
# (gptbridge_transport.priority_value_for) and shared_layer.adaptive.
_PRIORITY_VALUES: Final[dict[str, int]] = {
    "critical": 0,
    "interactive": 100,
    "background": 500,
    "maintenance": 900,
}


def normalize_priority_class(priority_class: str) -> str:
    normalized = str(priority_class or "interactive").strip().casefold()
    if normalized not in _PRIORITY_VALUES:
        raise ValueError(f"UNKNOWN_PRIORITY_CLASS:{priority_class}")
    return normalized


_normalize_priority_class = normalize_priority_class


__all__ = [
    "_CHANS",
    "_MAX_ID",
    "_MAX_BYTES",
    "_QUERY_TIMEOUT",
    "_POOL_MIN_CONN",
    "_POOL_MAX_CONN",
    "_POOL_TIMEOUT",
    "_POOL_IDLE_TTL_S",
    "_PRIORITY_VALUES",
    "_normalize_priority_class",
    "normalize_id",
    "normalize_priority_class",
]

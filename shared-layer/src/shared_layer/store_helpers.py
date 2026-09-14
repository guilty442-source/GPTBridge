"""Shared helpers for store.py and store_async.py (A185 split).

Kept here to avoid circular imports between store.py and store_async.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Final

from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
)

_CHANS: Final[frozenset[str]] = frozenset({"system", "ai"})
_MAX_ID: Final[int] = 256
_MAX_BYTES: Final[int] = 1_048_576
_QUERY_TIMEOUT: Final[float] = 10.0
_POOL_MIN_CONN: Final[int] = 2
_POOL_MAX_CONN: Final[int] = 10
_POOL_TIMEOUT: Final[float] = 5.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decode(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def encode_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise permission_denied() from exc
    if len(encoded.encode("utf-8")) > _MAX_BYTES:
        raise permission_denied()
    return encoded


def normalize_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > _MAX_ID or "`x00" in normalized:
        raise permission_denied()
    return normalized


__all__ = [
    "_CHANS",
    "_MAX_ID",
    "_MAX_BYTES",
    "_QUERY_TIMEOUT",
    "_POOL_MIN_CONN",
    "_POOL_MAX_CONN",
    "_POOL_TIMEOUT",
    "now_iso",
    "decode",
    "encode_json",
    "normalize_id",
]

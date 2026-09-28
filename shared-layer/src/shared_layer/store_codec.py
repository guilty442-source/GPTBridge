"""Serialization helpers for the governed PostgreSQL transport.

Split from ``store_helpers`` (source-size contract): this module owns the
timestamp/JSON codec surface while ``store_helpers`` keeps identity and
priority normalization plus the pool envelope constants.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
)

from .store_helpers import _MAX_BYTES


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


__all__ = ["now_iso", "decode", "encode_json"]

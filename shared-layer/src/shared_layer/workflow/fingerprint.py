"""Operation fingerprinting.

The fingerprint identifies "the same operation" across resends and retries,
so a duplicate submission maps to the existing workflow instead of starting
a second one.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .types import OperationFacts


def payload_hash(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        data = payload
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        import json

        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def operation_hash(facts: OperationFacts) -> str:
    source = "|".join(
        (
            facts.operation_type.strip(),
            facts.module_id.strip(),
            facts.resource_id.strip(),
            facts.revision.strip(),
            facts.payload_hash.strip(),
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def is_same_operation(existing_hash: str, facts: OperationFacts) -> bool:
    return bool(existing_hash) and existing_hash == operation_hash(facts)


__all__ = ["is_same_operation", "operation_hash", "payload_hash"]

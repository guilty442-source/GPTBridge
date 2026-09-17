"""Shared primitives for governed registry/queue schema migrations.

The migration plane (task §322-324 auto-sync registry, §348-350 canonical
tooling separation) keeps every schema step a *pure, deterministic,
idempotent* function: applying a step twice yields the same payload and the
second application is a NOOP (``schema_version`` already at/above the step's
target).  Digests are canonical (sorted keys, compact separators) so a step's
recorded ``input_digest``/``output_digest`` never depends on JSON formatting.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping


class SchemaError(ValueError):
    """Raised when a payload schema is invalid or has no migration step."""


def canonical_digest(payload: Mapping[str, Any]) -> str:
    """Deterministic sha256 over the canonical JSON form of ``payload``."""
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_schema(payload: Mapping[str, Any], *, default: int = 1) -> int:
    """Read ``schema_version``; legacy v1 files carry no explicit version."""
    raw = payload.get("schema_version", default)
    try:
        return int(raw)
    except (TypeError, ValueError) as error:
        raise SchemaError(f"invalid schema_version: {raw!r}") from error


def deep_copy(payload: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(payload)


@dataclass(frozen=True)
class MigrationStep:
    """One versioned schema step for one state target (registry or queue)."""

    migration_id: str
    target: str
    old_schema: int
    new_schema: int
    description: str
    apply: Callable[[dict[str, Any]], dict[str, Any]]

    def migrate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Idempotent step application: already-current payloads are NOOP."""
        if detect_schema(payload, default=self.old_schema) >= self.new_schema:
            return deep_copy(payload)
        return self.apply(deep_copy(payload))


__all__ = [
    "MigrationStep",
    "SchemaError",
    "canonical_digest",
    "deep_copy",
    "detect_schema",
]

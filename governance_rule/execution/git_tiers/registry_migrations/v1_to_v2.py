"""Schema v1 -> v2 steps for the supervisor registry and the merge queue.

Both state files currently start at an implicit v1 (no ``schema_version``):
the supervisor registry holds ``children`` watcher records and the merge
queue holds ``entries``.  v2 makes the schema explicit and stops treating
legacy files as untouched blobs:

* registry  — container types normalized, watcher records get typed defaults
  (``branch`` / ``worktree`` / ``log`` / ``pid`` / ``restarts`` /
  ``started_at``) and the file gains ``generation``.
* queue     — ``next_sequence`` and ``entries`` normalized to their loader
  contract and the file gains ``generation``.

Neither step reorders, renumbers, drops or rewrites existing values; the
merge-queue step never touches ``source_commit`` / ``base_main_commit`` or
any entry ordering field (no SHA recomputation, no re-sorting).
"""
from __future__ import annotations

from typing import Any

from .base import MigrationStep, SchemaError, deep_copy, detect_schema

REGISTRY_MIGRATION_ID = "registry-v1-to-v2"
QUEUE_MIGRATION_ID = "queue-v1-to-v2"

_CHILD_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("branch", ""),
    ("worktree", ""),
    ("log", ""),
    ("pid", 0),
    ("restarts", 0),
    ("started_at", 0.0),
)


def apply_registry_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: explicit schema/generation plus typed watcher defaults."""
    data = deep_copy(payload)
    if detect_schema(data) >= 2:
        return data
    children = data.get("children", [])
    if not isinstance(children, list):
        raise SchemaError("registry children must be a list")
    normalized: list[Any] = []
    for child in children:
        if not isinstance(child, dict):
            normalized.append(child)
            continue
        item = deep_copy(child)
        for key, default in _CHILD_DEFAULTS:
            item.setdefault(key, default)
        normalized.append(item)
    data["children"] = normalized
    data["schema_version"] = 2
    data["generation"] = int(data.get("generation", 0) or 0)
    return data


def apply_queue_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: explicit schema/generation with entry order preserved."""
    data = deep_copy(payload)
    if detect_schema(data) >= 2:
        return data
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise SchemaError("queue entries must be a list")
    try:
        next_sequence = int(data.get("next_sequence", 1))
    except (TypeError, ValueError) as error:
        raise SchemaError("queue next_sequence must be an integer") from error
    data["entries"] = deep_copy(entries)
    data["next_sequence"] = next_sequence
    data["schema_version"] = 2
    data["generation"] = int(data.get("generation", 0) or 0)
    return data


REGISTRY_STEP = MigrationStep(
    migration_id=REGISTRY_MIGRATION_ID,
    target="registry",
    old_schema=1,
    new_schema=2,
    description="explicit schema/generation + typed registry watcher defaults",
    apply=apply_registry_v1_to_v2,
)

QUEUE_STEP = MigrationStep(
    migration_id=QUEUE_MIGRATION_ID,
    target="queue",
    old_schema=1,
    new_schema=2,
    description="explicit schema/generation + normalized queue container",
    apply=apply_queue_v1_to_v2,
)


__all__ = [
    "QUEUE_MIGRATION_ID",
    "QUEUE_STEP",
    "REGISTRY_MIGRATION_ID",
    "REGISTRY_STEP",
    "apply_queue_v1_to_v2",
    "apply_registry_v1_to_v2",
]

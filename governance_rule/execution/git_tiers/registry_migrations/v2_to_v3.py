"""Schema v2 -> v3 steps: stable watcher identity and transaction binding.

v3 is the identity/transaction generation of the supervised automation plane:

* registry — each watcher record gains a deterministic ``watcher_id``
  (branch name, else the worktree directory name) and an explicit per-child
  ``state``; both are additions, never rewrites.
* queue    — each entry gains ``transaction_id`` (empty when unknown) and
  ``state`` mirrored from ``status`` when the entry predates v3.  A
  ``failed`` entry can never become ``pending``: ``status`` and an existing
  ``state`` are preserved byte-for-byte, as are priority, ordering and the
  ``source_commit`` / ``base_main_commit`` bindings (no SHA recomputation).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import MigrationStep, deep_copy, detect_schema

REGISTRY_MIGRATION_ID = "registry-v2-to-v3"
QUEUE_MIGRATION_ID = "queue-v2-to-v3"


def _watcher_identity(child: dict[str, Any]) -> str:
    branch = str(child.get("branch") or "").strip()
    if branch:
        return branch
    worktree = str(child.get("worktree") or "").strip()
    return Path(worktree).name if worktree else ""


def apply_registry_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """v2 -> v3: deterministic watcher identity plus explicit child state."""
    data = deep_copy(payload)
    if detect_schema(data) >= 3:
        return data
    children = data.get("children", [])
    if not isinstance(children, list):
        children = []
    for child in children:
        if not isinstance(child, dict):
            continue
        if not str(child.get("watcher_id") or "").strip():
            child["watcher_id"] = _watcher_identity(child)
        child.setdefault("state", "RUNNING")
    data["children"] = children
    data["schema_version"] = 3
    data.setdefault("generation", 0)
    return data


def apply_queue_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """v2 -> v3: transaction binding without touching SHA/order/priority."""
    data = deep_copy(payload)
    if detect_schema(data) >= 3:
        return data
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry.setdefault("transaction_id", "")
        if "state" not in entry:
            entry["state"] = str(entry.get("status") or "")
    data["entries"] = entries
    data["schema_version"] = 3
    data.setdefault("generation", 0)
    return data


REGISTRY_STEP = MigrationStep(
    migration_id=REGISTRY_MIGRATION_ID,
    target="registry",
    old_schema=2,
    new_schema=3,
    description="watcher_id/state identity binding for registry children",
    apply=apply_registry_v2_to_v3,
)

QUEUE_STEP = MigrationStep(
    migration_id=QUEUE_MIGRATION_ID,
    target="queue",
    old_schema=2,
    new_schema=3,
    description="transaction_id/state binding for merge-queue entries",
    apply=apply_queue_v2_to_v3,
)


__all__ = [
    "QUEUE_MIGRATION_ID",
    "QUEUE_STEP",
    "REGISTRY_MIGRATION_ID",
    "REGISTRY_STEP",
    "apply_queue_v2_to_v3",
    "apply_registry_v2_to_v3",
]

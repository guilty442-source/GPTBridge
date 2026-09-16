"""Crash-safe worker pool registry (task §66/§67).

Writes never touch ``registry.json`` in place:

    registry.tmp -> fsync -> atomic replace
    previous good copy kept at registry.previous

Startup with a corrupt current file reads ``registry.previous`` and
reconciles — never wipes slots.  Every registry carries
``schema_version`` / ``generation`` / ``updated_at``.

Schema migration (§67): ``migrate_registry`` upgrades v1 -> v2 ->
v3 step by step.  A registry from a *newer* schema is opened
read-only fail-safe — unknown fields are preserved, never dropped.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from .worker_pool_types import WorkerSlot

SCHEMA_VERSION: int = 2
REGISTRY_FILE = "registry.json"
PREVIOUS_FILE = "registry.previous"


class RegistryTooNewError(RuntimeError):
    """Registry written by a newer schema; opened read-only."""


def _slot_defaults_v2() -> dict[str, Any]:
    return {
        "attempt_id": "",
        "display_name": "",
        "domain": "",
        "parent_task_id": "",
        "merged_retained_until": 0.0,
        "retired_at": 0.0,
    }


def _migrate_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: workers gained attempt_id/domain/parent/retention."""
    workers = payload.get("workers", {})
    for worker in workers.values():
        for key, default in _slot_defaults_v2().items():
            worker.setdefault(key, default)
    payload["schema_version"] = 2
    return payload


_MIGRATIONS = {1: _migrate_v1_to_v2}


class PoolRegistry:
    """Atomic, versioned slot registry under the pool directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / REGISTRY_FILE
        self.previous_path = self.directory / PREVIOUS_FILE
        self.read_only = False

    def load(self) -> dict[str, Any]:
        """Load current registry; fall back to ``.previous`` on
        corruption; migrate older schemas; fail read-only on newer."""
        payload = self._read(self.path)
        if payload is None:
            payload = self._read(self.previous_path) or {}
        if not isinstance(payload, dict):
            payload = {}
        version = int(payload.get("schema_version", 0) or 0)
        if version > SCHEMA_VERSION:
            self.read_only = True
            return payload
        while version in _MIGRATIONS:
            payload = _MIGRATIONS[version](payload)
            version = int(payload.get("schema_version", 0) or 0)
        payload.setdefault("schema_version", SCHEMA_VERSION)
        payload.setdefault("generation", 0)
        payload.setdefault("workers", {})
        payload.setdefault("waiting", [])
        payload.setdefault("pool_state", "RUNNING")
        return payload

    def load_slots(self) -> dict[str, WorkerSlot]:
        payload = self.load()
        slots = {}
        for worker_id, data in payload.get("workers", {}).items():
            try:
                slots[worker_id] = WorkerSlot.from_dict(data)
            except (KeyError, ValueError, TypeError):
                continue
        return slots

    def store(self, payload: dict[str, Any]) -> None:
        """Atomically replace the registry (tmp -> fsync -> rename)."""
        if self.read_only:
            raise RegistryTooNewError(
                "registry schema newer than runtime; read-only"
            )
        payload["schema_version"] = SCHEMA_VERSION
        payload["generation"] = int(payload.get("generation", 0)) + 1
        payload["updated_at"] = time.time()
        tmp = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.tmp"
        )
        text = json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True
        )
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if self.path.is_file():
            try:
                os.replace(self.path, self.previous_path)
            except OSError:
                pass
        os.replace(tmp, self.path)

    def store_slots(
        self,
        slots: dict[str, WorkerSlot],
        *,
        payload: Optional[dict[str, Any]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        data = payload if payload is not None else self.load()
        data["workers"] = {
            wid: slot.to_dict() for wid, slot in slots.items()
        }
        if extra:
            data.update(extra)
        self.store(data)

    def _read(self, path: Path) -> Optional[dict[str, Any]]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


__all__ = [
    "PREVIOUS_FILE",
    "REGISTRY_FILE",
    "SCHEMA_VERSION",
    "PoolRegistry",
    "RegistryTooNewError",
]

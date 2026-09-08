"""registry_repository — codex-native local resource registry cache on sqlite3.

Stdlib-only local cache for module-private resource locators (A35/E21 +
A37/E23 + A44/E30).  The canonical central registry lives in PostgreSQL;
this repository is a bounded, owner-private cache and must not be treated as
authoritative outside its module scope.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LocationRecord:
    locator_id: uuid.UUID
    resource_id: str
    module_id: str
    executor_type: str
    location_key: str
    physical_location: str
    status: str = "active"


@dataclass(frozen=True)
class ResourceRecord:
    resource_id: str
    platform_id: str
    module_id: str
    owner_id: str
    data_category: str
    resource_type: str
    resource_label: str
    logical_key: str
    classification: str
    locator_id: uuid.UUID
    content_hash: str | None = None
    metadata: dict[str, Any] | None = None


class LocalResourceRegistry:
    """Central registry. Physical locations never leave this repository API."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS resource (
                   resource_id TEXT NOT NULL PRIMARY KEY,
                   platform_id TEXT NOT NULL,
                   module_id TEXT NOT NULL,
                   owner_id TEXT NOT NULL,
                   data_category TEXT NOT NULL,
                   resource_type TEXT NOT NULL,
                   resource_label TEXT NOT NULL,
                   logical_key TEXT NOT NULL,
                   classification TEXT NOT NULL,
                   locator_id TEXT NOT NULL,
                   content_hash TEXT,
                   metadata TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS locations (
                   locator_id TEXT NOT NULL PRIMARY KEY,
                   resource_id TEXT NOT NULL UNIQUE,
                   module_id TEXT NOT NULL,
                   executor_type TEXT NOT NULL,
                   location_key TEXT NOT NULL,
                   physical_location TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active',
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_locations_resolve "
            "ON locations (resource_id, executor_type, status)"
        )

    def register_location(self, record: LocationRecord) -> None:
        if not record.physical_location or "\x00" in record.physical_location:
            raise ValueError("INVALID_PHYSICAL_LOCATION")
        self.connection.execute(
            """INSERT INTO locations (locator_id,resource_id,module_id,executor_type,location_key,physical_location,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,datetime('now'),datetime('now'))
               ON CONFLICT (resource_id) DO UPDATE SET
                 locator_id=excluded.locator_id, executor_type=excluded.executor_type,
                 location_key=excluded.location_key, physical_location=excluded.physical_location,
                 status=excluded.status, updated_at=datetime('now')""",
            (str(record.locator_id), record.resource_id, record.module_id, record.executor_type,
             record.location_key, record.physical_location, record.status),
        )

    def resolve_for_executor(self, resource_id: str, executor_type: str) -> LocationRecord | None:
        row = self.connection.execute(
            """SELECT locator_id,resource_id,module_id,executor_type,location_key,physical_location,status
               FROM locations WHERE resource_id=? AND executor_type=? AND status='active'""",
            (resource_id, executor_type),
        ).fetchone()
        if row is None:
            return None
        return LocationRecord(
            locator_id=uuid.UUID(str(row[0])),
            resource_id=str(row[1]),
            module_id=str(row[2]),
            executor_type=str(row[3]),
            location_key=str(row[4]),
            physical_location=str(row[5]),
            status=str(row[6]),
        )

    def upsert_resource(self, record: ResourceRecord) -> None:
        self.connection.execute(
            """INSERT INTO resource (resource_id,platform_id,module_id,owner_id,data_category,resource_type,resource_label,logical_key,classification,locator_id,content_hash,metadata,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
               ON CONFLICT(resource_id) DO UPDATE SET
                 logical_key=excluded.logical_key, classification=excluded.classification,
                 content_hash=excluded.content_hash, metadata=excluded.metadata, updated_at=datetime('now')""",
            (record.resource_id, record.platform_id, record.module_id, record.owner_id,
             record.data_category, record.resource_type, record.resource_label, record.logical_key,
             record.classification, str(record.locator_id), record.content_hash,
             None if record.metadata is None else json.dumps(record.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )

    def resource_exists(self, resource_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM resource WHERE resource_id=?",
            (resource_id,),
        ).fetchone()
        return row is not None


ResourceRegistry = LocalResourceRegistry

__all__ = ["LocationRecord", "LocalResourceRegistry", "ResourceRecord", "ResourceRegistry"]
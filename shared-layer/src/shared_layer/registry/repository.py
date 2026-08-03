from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from psycopg import Connection


@dataclass(frozen=True)
class LocationRecord:
    locator_id: uuid.UUID
    resource_id: str
    module_id: str
    executor_type: str
    location_key: str
    physical_location: str
    status: str = "active"


class ResourceRegistry:
    """Central registry. Physical locations never leave this repository API."""

    def __init__(self, connection: Connection[dict[str, Any]]) -> None:
        self.connection = connection

    def register_location(self, record: LocationRecord) -> None:
        if not record.physical_location or "\x00" in record.physical_location:
            raise ValueError("INVALID_PHYSICAL_LOCATION")
        self.connection.execute(
            """INSERT INTO registry.locations
               (locator_id,resource_id,module_id,executor_type,location_key,physical_location,status)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (resource_id) DO UPDATE SET
                 locator_id=excluded.locator_id, executor_type=excluded.executor_type,
                 location_key=excluded.location_key, physical_location=excluded.physical_location,
                 status=excluded.status, updated_at=now()""",
            (record.locator_id, record.resource_id, record.module_id, record.executor_type,
             record.location_key, record.physical_location, record.status),
        )

    def resolve_for_executor(self, resource_id: str, module_id: str) -> LocationRecord | None:
        row = self.connection.execute(
            """SELECT locator_id,resource_id,module_id,executor_type,location_key,
                      physical_location,status
               FROM registry.locations
               WHERE resource_id=%s AND module_id=%s AND status='active'""",
            (resource_id, module_id),
        ).fetchone()
        return None if not row else LocationRecord(**row)


__all__ = ["LocationRecord", "ResourceRegistry"]

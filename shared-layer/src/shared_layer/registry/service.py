from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection

from ..resource_identity import ResourceIdentity
from .repository import LocationRecord, ResourceRegistry


@dataclass(frozen=True)
class RegisterResource:
    identity: ResourceIdentity
    logical_key: str
    classification: str
    executor_type: str
    location_key: str
    physical_location: str
    content_hash: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class RegistryService:
    """Atomically creates the central resource row and its opaque locator."""

    def __init__(self, connection: Connection[dict[str, Any]]) -> None:
        self.connection = connection

    def register(self, command: RegisterResource) -> uuid.UUID:
        identity = command.identity
        locator_id = uuid.uuid5(uuid.NAMESPACE_URL, f"gptbridge:{identity.module_id}:{identity.resource_id}")
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO gptbridge_index.resource
                   (resource_id,platform_id,module_id,owner_id,data_category,resource_type,
                    resource_label,logical_key,classification,locator_id,content_hash,metadata)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(resource_id) DO UPDATE SET
                     logical_key=excluded.logical_key,classification=excluded.classification,
                     content_hash=excluded.content_hash,metadata=excluded.metadata,updated_at=now()""",
                (identity.resource_id, identity.platform_id, identity.module_id, identity.owner_id,
                 identity.data_category, identity.resource_type, identity.label, command.logical_key,
                 command.classification, locator_id, command.content_hash, command.metadata),
            )
            ResourceRegistry(self.connection).register_location(LocationRecord(
                locator_id=locator_id,
                resource_id=identity.resource_id,
                module_id=identity.module_id,
                executor_type=command.executor_type,
                location_key=command.location_key,
                physical_location=command.physical_location,
            ))
        return locator_id


__all__ = ["RegisterResource", "RegistryService"]

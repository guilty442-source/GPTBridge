from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from sqlite3 import Connection
from typing import Any

from ..resource_identity import ResourceIdentity, locator_id_for
from ..local.registry_repository import LocationRecord, ResourceRecord, ResourceRegistry


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

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        ResourceRegistry._ensure_schema(connection)

    def register(self, command: RegisterResource) -> uuid.UUID:
        identity = command.identity
        locator_id = locator_id_for(identity.module_id, identity.resource_id)
        with self.connection:
            registry = ResourceRegistry(self.connection)
            registry.upsert_resource(ResourceRecord(
                resource_id=identity.resource_id,
                platform_id=identity.platform_id,
                module_id=identity.module_id,
                owner_id=identity.owner_id,
                data_category=identity.data_category,
                resource_type=identity.resource_type,
                resource_label=identity.label,
                logical_key=command.logical_key,
                classification=command.classification,
                locator_id=locator_id,
                content_hash=command.content_hash,
                metadata=dict(command.metadata or {}),
            ))
            registry.register_location(LocationRecord(
                locator_id=locator_id,
                resource_id=identity.resource_id,
                module_id=identity.module_id,
                executor_type=command.executor_type,
                location_key=command.location_key,
                physical_location=command.physical_location,
            ))
        return locator_id


__all__ = ["RegisterResource", "RegistryService"]
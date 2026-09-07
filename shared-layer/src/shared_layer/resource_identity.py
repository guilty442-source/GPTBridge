from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Final


PLATFORM_ID: Final[str] = "local-model-platform"
XINGCHENG_MODULE_ID: Final[str] = "xingcheng"
_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Canonical namespace prefix for locator_id generation (A8/E21).
_LOCATOR_NAMESPACE: Final[str] = "gptbridge"

#: Canonical namespace prefix for RAG point_id generation (A52/E38).
_POINT_NAMESPACE: Final[str] = "gptbridge-rag"


def canonical_identifier(value: str, *, field: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"INVALID_{field.upper()}")
    return normalized


def locator_id_for(module_id: str, resource_id: str) -> uuid.UUID:
    """Canonical opaque locator_id for a resource (A8/E21).

    Formula: uuid5(NAMESPACE_URL, f"gptbridge:{module_id}:{resource_id}")

    All modules, RAG pipelines, and identity stores MUST use this function
    to derive locator_id from (module_id, resource_id).  Physical locations
    are never exposed; only this opaque UUID is stored in the central index.
    """
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{_LOCATOR_NAMESPACE}:{module_id}:{resource_id}",
    )


def point_id_for(chunk_id: str) -> uuid.UUID:
    """Canonical Qdrant point_id for a RAG chunk (A52/E38).

    Formula: uuid5(NAMESPACE_URL, f"gptbridge-rag:{chunk_id}")
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{_POINT_NAMESPACE}:{chunk_id}")


@dataclass(frozen=True)
class ResourceIdentity:
    """Canonical identity shared by local sqlite, RAG, vector and audit records."""

    module_id: str
    data_category: str
    resource_type: str
    resource_id: str
    owner_id: str | None = None
    platform_id: str = PLATFORM_ID

    def __post_init__(self) -> None:
        for field in (
            "platform_id",
            "module_id",
            "data_category",
            "resource_type",
            "resource_id",
        ):
            object.__setattr__(
                self,
                field,
                canonical_identifier(getattr(self, field), field=field),
            )
        owner = self.owner_id or self.module_id
        object.__setattr__(
            self, "owner_id", canonical_identifier(owner, field="owner_id")
        )

    @property
    def label(self) -> str:
        return ":".join(
            (
                self.platform_id,
                self.module_id,
                self.data_category,
                self.resource_type,
                self.resource_id,
            )
        )

    def as_tags(self) -> dict[str, str]:
        return {
            "platform_id": self.platform_id,
            "module_id": self.module_id,
            "owner_id": str(self.owner_id),
            "data_category": self.data_category,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_label": self.label,
        }


__all__ = [
    "PLATFORM_ID",
    "ResourceIdentity",
    "XINGCHENG_MODULE_ID",
    "canonical_identifier",
    "locator_id_for",
    "point_id_for",
]

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


PLATFORM_ID: Final[str] = "local-model-platform"
XINGCHENG_MODULE_ID: Final[str] = "xingcheng"
_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def canonical_identifier(value: str, *, field: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"INVALID_{field.upper()}")
    return normalized


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
]

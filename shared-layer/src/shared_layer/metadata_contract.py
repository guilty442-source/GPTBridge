"""metadata_contract — canonical resource metadata fields for all modules.

Every module (PostgreSQL central index, SQLite local stores, Qdrant payloads,
audit records, transport requests) MUST use these fixed fields when describing
a resource.  This contract is the single source of truth for field names,
types, and semantics (A8/E21 + A44/E30).

Fixed fields:
    module_id       str        owning module (kebab-case, canonical_identifier)
    resource_id     str        module-scoped resource key (kebab-case)
    locator_id      uuid.UUID  opaque locator (locator_id_for(module_id, resource_id))
    version         int        monotonic version counter (>= 1)
    content_hash    str|None   SHA-256 hex digest of payload, or None
    updated_at      str        ISO-8601 timestamp of last mutation
    status          str        lifecycle state (see STATUS_VALUES)

Data ownership contract:
    PostgreSQL  = central index & relation truth
    Qdrant      = rebuildable vector index (never the sole copy of unique data)
    SQLite/NTFS = original or module-private data
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from .resource_identity import (
    ResourceIdentity,
    canonical_identifier,
    locator_id_for,
)


# ---------------------------------------------------------------------------
# Canonical field names — every store MUST use exactly these column names.
# ---------------------------------------------------------------------------

FIELD_MODULE_ID: Final[str] = "module_id"
FIELD_RESOURCE_ID: Final[str] = "resource_id"
FIELD_LOCATOR_ID: Final[str] = "locator_id"
FIELD_VERSION: Final[str] = "version"
FIELD_CONTENT_HASH: Final[str] = "content_hash"
FIELD_UPDATED_AT: Final[str] = "updated_at"
FIELD_CREATED_AT: Final[str] = "created_at"
FIELD_STATUS: Final[str] = "status"
FIELD_PLATFORM_ID: Final[str] = "platform_id"
FIELD_OWNER_ID: Final[str] = "owner_id"
FIELD_DATA_CATEGORY: Final[str] = "data_category"
FIELD_RESOURCE_TYPE: Final[str] = "resource_type"
FIELD_RESOURCE_LABEL: Final[str] = "resource_label"
FIELD_LOGICAL_KEY: Final[str] = "logical_key"
FIELD_CLASSIFICATION: Final[str] = "classification"
FIELD_INDEX_STATUS: Final[str] = "index_status"
FIELD_METADATA: Final[str] = "metadata"
FIELD_INTEGRITY_HASH: Final[str] = "integrity_hash"
FIELD_SCHEMA_VERSION: Final[str] = "schema_version"

REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    FIELD_MODULE_ID,
    FIELD_RESOURCE_ID,
    FIELD_LOCATOR_ID,
    FIELD_VERSION,
    FIELD_CONTENT_HASH,
    FIELD_UPDATED_AT,
    FIELD_STATUS,
)

# ---------------------------------------------------------------------------
# Status value vocabulary — shared across all stores.
# ---------------------------------------------------------------------------

STATUS_ACTIVE: Final[str] = "active"
STATUS_INDEXED: Final[str] = "indexed"
STATUS_PENDING: Final[str] = "pending"
STATUS_MISSING: Final[str] = "missing"
STATUS_MOVING: Final[str] = "moving"
STATUS_ARCHIVED: Final[str] = "archived"
STATUS_DELETED: Final[str] = "deleted"
STATUS_REFERENCED: Final[str] = "referenced"

STATUS_VALUES: Final[frozenset[str]] = frozenset({
    STATUS_ACTIVE,
    STATUS_INDEXED,
    STATUS_PENDING,
    STATUS_MISSING,
    STATUS_MOVING,
    STATUS_ARCHIVED,
    STATUS_DELETED,
    STATUS_REFERENCED,
})

# ---------------------------------------------------------------------------
# Classification vocabulary — sensitivity tiers for RLS and access control.
# ---------------------------------------------------------------------------

CLASSIFICATION_PUBLIC: Final[str] = "public"
CLASSIFICATION_INTERNAL: Final[str] = "internal"
CLASSIFICATION_PRIVATE: Final[str] = "private"
CLASSIFICATION_PERMISSION: Final[str] = "permission"
CLASSIFICATION_PERMISSION_FILE: Final[str] = "permission-file"
CLASSIFICATION_PERMISSION_DIRECTORY: Final[str] = "permission-directory"
CLASSIFICATION_GOVERNANCE_RULE: Final[str] = "governance-rule"

#: Classifications that Xingcheng is forbidden from writing (E30/A49).
XINGCHENG_FORBIDDEN_CLASSIFICATIONS: Final[frozenset[str]] = frozenset({
    CLASSIFICATION_PERMISSION,
    CLASSIFICATION_PERMISSION_FILE,
    CLASSIFICATION_PERMISSION_DIRECTORY,
    CLASSIFICATION_GOVERNANCE_RULE,
})

# ---------------------------------------------------------------------------
# Data ownership tiers — which store is authoritative for which data.
# ---------------------------------------------------------------------------

TIER_POSTGRESQL: Final[str] = "postgresql"
TIER_QDRANT: Final[str] = "qdrant"
TIER_SQLITE_NTFS: Final[str] = "sqlite-ntfs"

DATA_OWNERSHIP: Final[dict[str, str]] = {
    TIER_POSTGRESQL: "central-index-and-relation-truth",
    TIER_QDRANT: "rebuildable-vector-index-never-sole-copy",
    TIER_SQLITE_NTFS: "original-or-module-private-data",
}

# ---------------------------------------------------------------------------
# Metadata contract dataclass — validates all fixed fields.
# ---------------------------------------------------------------------------

_VERSION_PATTERN = re.compile(r"^[1-9][0-9]*$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$|^$")


@dataclass(frozen=True)
class ResourceMetadata:
    """Canonical resource metadata — the single contract across all stores.

    Every PostgreSQL row, SQLite row, Qdrant payload, and audit record that
    describes a resource MUST populate these fields.  Stores MAY add
    store-specific fields, but these fixed fields are mandatory and their
    names/types MUST NOT diverge.
    """

    module_id: str
    resource_id: str
    locator_id: str  # uuid5 string form
    version: int
    updated_at: str  # ISO-8601
    status: str
    content_hash: str | None = None
    created_at: str | None = None  # ISO-8601, optional for legacy rows
    platform_id: str | None = None
    owner_id: str | None = None
    data_category: str | None = None
    resource_type: str | None = None
    resource_label: str | None = None
    logical_key: str | None = None
    classification: str | None = None
    index_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate required fields
        for name in (FIELD_MODULE_ID, FIELD_RESOURCE_ID, FIELD_LOCATOR_ID,
                     FIELD_VERSION, FIELD_UPDATED_AT, FIELD_STATUS):
            value = getattr(self, name)
            if value is None or value == "":
                raise ValueError(f"METADATA_CONTRACT_MISSING_{name.upper()}")
        # Validate module_id / resource_id format
        object.__setattr__(self, FIELD_MODULE_ID,
                           canonical_identifier(self.module_id, field="module_id"))
        object.__setattr__(self, FIELD_RESOURCE_ID,
                           canonical_identifier(self.resource_id, field="resource_id"))
        # Validate version
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError("METADATA_CONTRACT_INVALID_VERSION")
        # Validate status
        if self.status not in STATUS_VALUES:
            raise ValueError(f"METADATA_CONTRACT_INVALID_STATUS:{self.status}")
        # Validate content_hash format if present
        if self.content_hash is not None and self.content_hash != "":
            if not _HASH_PATTERN.fullmatch(self.content_hash):
                raise ValueError("METADATA_CONTRACT_INVALID_CONTENT_HASH")
        # Validate locator_id matches canonical formula
        expected = str(locator_id_for(self.module_id, self.resource_id))
        if self.locator_id != expected:
            raise ValueError("METADATA_CONTRACT_LOCATOR_ID_MISMATCH")

    @classmethod
    def from_identity(
        cls,
        identity: ResourceIdentity,
        *,
        version: int,
        updated_at: str,
        status: str,
        content_hash: str | None = None,
        created_at: str | None = None,
        logical_key: str | None = None,
        classification: str | None = None,
        index_status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ResourceMetadata:
        """Build metadata from a ResourceIdentity + lifecycle fields."""
        return cls(
            module_id=identity.module_id,
            resource_id=identity.resource_id,
            locator_id=str(locator_id_for(identity.module_id, identity.resource_id)),
            version=version,
            updated_at=updated_at,
            status=status,
            content_hash=content_hash,
            created_at=created_at,
            platform_id=identity.platform_id,
            owner_id=identity.owner_id,
            data_category=identity.data_category,
            resource_type=identity.resource_type,
            resource_label=identity.label,
            logical_key=logical_key,
            classification=classification,
            index_status=index_status,
            metadata=dict(metadata or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize to a flat dict with canonical field names."""
        result: dict[str, Any] = {
            FIELD_MODULE_ID: self.module_id,
            FIELD_RESOURCE_ID: self.resource_id,
            FIELD_LOCATOR_ID: self.locator_id,
            FIELD_VERSION: self.version,
            FIELD_CONTENT_HASH: self.content_hash,
            FIELD_UPDATED_AT: self.updated_at,
            FIELD_STATUS: self.status,
        }
        if self.created_at is not None:
            result[FIELD_CREATED_AT] = self.created_at
        if self.platform_id is not None:
            result[FIELD_PLATFORM_ID] = self.platform_id
        if self.owner_id is not None:
            result[FIELD_OWNER_ID] = self.owner_id
        if self.data_category is not None:
            result[FIELD_DATA_CATEGORY] = self.data_category
        if self.resource_type is not None:
            result[FIELD_RESOURCE_TYPE] = self.resource_type
        if self.resource_label is not None:
            result[FIELD_RESOURCE_LABEL] = self.resource_label
        if self.logical_key is not None:
            result[FIELD_LOGICAL_KEY] = self.logical_key
        if self.classification is not None:
            result[FIELD_CLASSIFICATION] = self.classification
        if self.index_status is not None:
            result[FIELD_INDEX_STATUS] = self.index_status
        if self.metadata:
            result[FIELD_METADATA] = self.metadata
        return result

    def as_tags(self) -> dict[str, str]:
        """Flat string tags for Qdrant payload (all values as strings)."""
        tags: dict[str, str] = {
            FIELD_MODULE_ID: self.module_id,
            FIELD_RESOURCE_ID: self.resource_id,
            FIELD_LOCATOR_ID: self.locator_id,
            FIELD_VERSION: str(self.version),
            FIELD_UPDATED_AT: self.updated_at,
            FIELD_STATUS: self.status,
        }
        if self.content_hash is not None:
            tags[FIELD_CONTENT_HASH] = self.content_hash
        if self.resource_type is not None:
            tags[FIELD_RESOURCE_TYPE] = self.resource_type
        if self.resource_label is not None:
            tags[FIELD_RESOURCE_LABEL] = self.resource_label
        if self.classification is not None:
            tags[FIELD_CLASSIFICATION] = self.classification
        return tags


# ---------------------------------------------------------------------------
# Qdrant payload contract — every vector point MUST carry these tags.
# ---------------------------------------------------------------------------

QDRANT_REQUIRED_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    FIELD_MODULE_ID,
    FIELD_RESOURCE_ID,
    "chunk_id",
    FIELD_VERSION,
    FIELD_LOCATOR_ID,
)


def validate_qdrant_payload(payload: dict[str, Any]) -> list[str]:
    """Validate a Qdrant point payload against the metadata contract.

    Returns a list of violation messages (empty = valid).
    """
    violations: list[str] = []
    for name in QDRANT_REQUIRED_PAYLOAD_FIELDS:
        if name not in payload or payload[name] in (None, ""):
            violations.append(f"QDRANT_PAYLOAD_MISSING:{name}")
    # module_id + resource_id must both be present (dual filter requirement)
    if FIELD_MODULE_ID not in payload or FIELD_RESOURCE_ID not in payload:
        violations.append("QDRANT_PAYLOAD_DUAL_FILTER_REQUIRED")
    # version must be a positive integer
    version = payload.get(FIELD_VERSION)
    if version is not None:
        try:
            if int(version) < 1:
                violations.append("QDRANT_PAYLOAD_INVALID_VERSION")
        except (TypeError, ValueError):
            violations.append("QDRANT_PAYLOAD_INVALID_VERSION")
    return violations


__all__ = [
    "CLASSIFICATION_GOVERNANCE_RULE",
    "CLASSIFICATION_INTERNAL",
    "CLASSIFICATION_PERMISSION",
    "CLASSIFICATION_PERMISSION_DIRECTORY",
    "CLASSIFICATION_PERMISSION_FILE",
    "CLASSIFICATION_PRIVATE",
    "CLASSIFICATION_PUBLIC",
    "DATA_OWNERSHIP",
    "FIELD_CLASSIFICATION",
    "FIELD_CONTENT_HASH",
    "FIELD_CREATED_AT",
    "FIELD_DATA_CATEGORY",
    "FIELD_INTEGRITY_HASH",
    "FIELD_INDEX_STATUS",
    "FIELD_LOCATOR_ID",
    "FIELD_LOGICAL_KEY",
    "FIELD_METADATA",
    "FIELD_MODULE_ID",
    "FIELD_OWNER_ID",
    "FIELD_PLATFORM_ID",
    "FIELD_RESOURCE_ID",
    "FIELD_RESOURCE_LABEL",
    "FIELD_RESOURCE_TYPE",
    "FIELD_SCHEMA_VERSION",
    "FIELD_STATUS",
    "FIELD_UPDATED_AT",
    "FIELD_VERSION",
    "QDRANT_REQUIRED_PAYLOAD_FIELDS",
    "REQUIRED_FIELDS",
    "ResourceMetadata",
    "STATUS_ACTIVE",
    "STATUS_ARCHIVED",
    "STATUS_DELETED",
    "STATUS_INDEXED",
    "STATUS_MISSING",
    "STATUS_MOVING",
    "STATUS_PENDING",
    "STATUS_REFERENCED",
    "STATUS_VALUES",
    "TIER_POSTGRESQL",
    "TIER_QDRANT",
    "TIER_SQLITE_NTFS",
    "XINGCHENG_FORBIDDEN_CLASSIFICATIONS",
    "validate_qdrant_payload",
]

"""Qdrant query scoping.

Vector queries must be scoped at the data layer, not by caller discipline:

  * ``module_id`` is mandatory on every search/delete;
  * optional ``resource_id`` / ``classification`` / ``revision`` narrow it;
  * an empty module scope is rejected (fail closed), so a forgotten filter
    can never turn into a whole-collection scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

REQUIRED_PAYLOAD_FIELDS: Final[tuple[str, ...]] = ("module_id",)


class QdrantScopeError(RuntimeError):
    """Raised when a Qdrant operation is not properly scoped."""


@dataclass(frozen=True)
class ScopedFilter:
    module_ids: tuple[str, ...]
    resource_id: str = ""
    classification: str = ""
    revision: str = ""
    additional: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        cleaned = tuple(str(value).strip() for value in self.module_ids if str(value).strip())
        if not cleaned:
            raise QdrantScopeError("QDRANT_MODULE_SCOPE_REQUIRED")
        object.__setattr__(self, "module_ids", cleaned)

    def to_qdrant(self) -> dict[str, Any]:
        must: list[dict[str, Any]] = [
            {"key": "module_id", "match": {"any": list(self.module_ids)}}
        ]
        if self.resource_id:
            must.append({"key": "resource_id", "match": {"value": self.resource_id}})
        if self.classification:
            must.append({"key": "classification", "match": {"value": self.classification}})
        if self.revision:
            must.append({"key": "revision", "match": {"value": self.revision}})
        for key, value in self.additional.items():
            must.append({"key": str(key), "match": {"value": value}})
        return {"must": must}

    def assert_validated(self) -> None:
        if not self.resource_id:
            return
        if not self.classification and not self.revision:
            return
        for field_name in REQUIRED_PAYLOAD_FIELDS:
            if not any(entry.get("key") == field_name for entry in self.to_qdrant()["must"]):
                raise QdrantScopeError(f"QDRANT_REQUIRED_FILTER_MISSING:{field_name}")


def require_scope(
    module_ids: list[str] | tuple[str, ...] | None,
    *,
    resource_id: str = "",
    classification: str = "",
    revision: str = "",
    additional: dict[str, Any] | None = None,
) -> ScopedFilter:
    """Fail-closed constructor used by runtime call sites."""
    if not module_ids:
        raise QdrantScopeError("QDRANT_MODULE_SCOPE_REQUIRED")
    scope = ScopedFilter(
        module_ids=tuple(module_ids),
        resource_id=resource_id,
        classification=classification,
        revision=revision,
        additional=additional or {},
    )
    scope.assert_validated()
    return scope


def assert_payload_scoped(payload: dict[str, Any]) -> None:
    """Upserts must carry the mandatory payload fields."""
    missing = [name for name in REQUIRED_PAYLOAD_FIELDS if not str(payload.get(name) or "").strip()]
    if missing:
        raise QdrantScopeError("QDRANT_PAYLOAD_SCOPE_MISSING:" + ",".join(missing))


__all__ = [
    "REQUIRED_PAYLOAD_FIELDS",
    "QdrantScopeError",
    "ScopedFilter",
    "assert_payload_scoped",
    "require_scope",
]

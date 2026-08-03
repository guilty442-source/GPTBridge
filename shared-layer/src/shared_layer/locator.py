from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final

from psycopg import Connection

from governance_rule.execution.authentication import GovernanceAuthenticationService
from governance_rule.permission_directory.execution.path_guard import permission_denied


_MODULE_ID: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
OwnerResolver = Callable[[uuid.UUID, str], str | None]


@dataclass(frozen=True)
class ResolvedOwnerResource:
    module_id: str
    resource_id: str
    locator_id: uuid.UUID
    relative_path: str


class GovernedLocatorResolver:
    """Resolves opaque locators only after governance and owner verification."""

    def __init__(self, project_root: Path | str, authentication: GovernanceAuthenticationService) -> None:
        if not isinstance(authentication, GovernanceAuthenticationService):
            raise permission_denied()
        self._project_root = Path(project_root).resolve()
        self._authentication = authentication
        self._owners: dict[str, OwnerResolver] = {}

    def register_owner(self, module_id: str, resolver: OwnerResolver) -> None:
        normalized = str(module_id or "").strip().casefold()
        if not _MODULE_ID.fullmatch(normalized) or not callable(resolver):
            raise permission_denied()
        self._owners[normalized] = resolver

    def resolve(self, token: str, *, module_id: str, resource_id: str, locator_id: str) -> ResolvedOwnerResource:
        normalized = str(module_id or "").strip().casefold()
        claims = self._authentication.authenticate_token(token)
        if (
            not _MODULE_ID.fullmatch(normalized)
            or claims.capability != "star-global-data-read"
            or claims.action != "request-read"
            or claims.target != f"owner-resource-resolve:{normalized}"
            or claims.data_scope != "opaque-locator-id-only"
            or claims.resource_path != locator_id
        ):
            raise permission_denied()
        try:
            opaque_locator = uuid.UUID(str(locator_id))
        except ValueError as exc:
            raise permission_denied() from exc
        owner = self._owners.get(normalized)
        if owner is None:
            raise permission_denied()
        relative = owner(opaque_locator, str(resource_id))
        if not relative:
            raise permission_denied()
        candidate = (self._project_root / normalized / relative).resolve()
        owner_root = (self._project_root / normalized).resolve()
        try:
            verified_relative = candidate.relative_to(owner_root)
        except ValueError as exc:
            raise permission_denied() from exc
        return ResolvedOwnerResource(normalized, str(resource_id), opaque_locator, verified_relative.as_posix())


class RegistryLocatorResolver:
    """Resolve a resource through the central registry only for its selected executor.

    The returned location is an internal executor value and must never be serialized
    into a module-to-module message.
    """

    def __init__(self, project_root: Path | str, connection: Connection[dict[str, Any]]) -> None:
        self._project_root = Path(project_root).resolve()
        self._connection = connection

    def resolve_for_executor(self, *, resource_id: str, executor_type: str) -> ResolvedOwnerResource:
        row = self._connection.execute(
            """SELECT locator_id,resource_id,module_id,physical_location
               FROM registry.locations
               WHERE resource_id=%s AND executor_type=%s AND status='active'""",
            (resource_id, executor_type),
        ).fetchone()
        if not row:
            raise permission_denied()
        module_id = str(row["module_id"])
        owner_root = (self._project_root / module_id / "data").resolve()
        candidate = Path(str(row["physical_location"])).resolve()
        try:
            relative = candidate.relative_to(owner_root)
        except ValueError as exc:
            raise permission_denied() from exc
        return ResolvedOwnerResource(module_id, str(row["resource_id"]), row["locator_id"], relative.as_posix())


__all__ = ["GovernedLocatorResolver", "RegistryLocatorResolver", "ResolvedOwnerResource"]

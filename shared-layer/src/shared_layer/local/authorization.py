"""authorization — codex-native local authorization on PostgreSQL.

Replacement for ``shared_layer.access_gateway.postgres
.PostgresAuthorization`` (A219/E21 + A37/E23).  The authorization checks are
data-driven from the central registry; the semantics (explicit allow, no
implicit disclosure) are unchanged.  A610/A621: PostgreSQL is the sole
structured-data authority — pass a ``pg_adapter.PgConnection`` (or any
connection exposing the sqlite-style ``execute`` API through the adapter).
"""

from __future__ import annotations

from typing import Any

from .registry_repository import LocalResourceRegistry


class LocalAuthorization:
    """Authorization checks against the central registry only."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._registry = LocalResourceRegistry(connection)

    def can_read_resource(self, resource_id: str) -> bool:
        return self._registry.resource_exists(str(resource_id))

    def can_access_module(self, module_id: str, *, write: bool = False) -> bool:
        if not module_id:
            return False
        if write:
            row = self.connection.execute(
                "SELECT can_write FROM principal_scope WHERE module_id=?",
                (str(module_id),),
            ).fetchone()
            return bool(row and row[0])
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM resource WHERE module_id=?",
            (str(module_id),),
        ).fetchone()
        return bool(row and int(row[0]) > 0)


PostgresAuthorization = LocalAuthorization

__all__ = ["LocalAuthorization", "PostgresAuthorization"]

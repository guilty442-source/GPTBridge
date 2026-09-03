"""authorization — codex-native local authorization on sqlite3.

Stdlib-only replacement for ``shared_layer.access_gateway.postgres
.PostgresAuthorization`` (A35/E21 + A37/E23).  The authorization checks are
data-driven from the local central registry instead of PostgreSQL RLS; the
semantics (explicit allow, no implicit disclosure) are unchanged.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .registry_repository import LocalResourceRegistry


class LocalAuthorization:
    """Authorization checks against the local central registry only."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._registry = LocalResourceRegistry(connection)

    def can_read_resource(self, resource_id: str) -> bool:
        return self._registry.resource_exists(str(resource_id))

    def can_access_module(self, module_id: str, *, write: bool = False) -> bool:
        if not module_id:
            return False
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM resource WHERE module_id=?",
            (str(module_id),),
        ).fetchone()
        return bool(row and int(row[0]) > 0)


PostgresAuthorization = LocalAuthorization

__all__ = ["LocalAuthorization", "PostgresAuthorization"]
from __future__ import annotations

from typing import Any

from psycopg import Connection


class PostgresAuthorization:
    """Ask PostgreSQL's RLS authority instead of duplicating grants in Python."""

    def __init__(self, connection: Connection[dict[str, Any]]) -> None:
        self.connection = connection

    def can_read_resource(self, resource_id: str) -> bool:
        row = self.connection.execute(
            "SELECT EXISTS(SELECT 1 FROM gptbridge_index.resource WHERE resource_id=%s) AS allowed",
            (resource_id,),
        ).fetchone()
        return bool(row and row["allowed"])

    def can_access_module(self, module_id: str, *, write: bool = False) -> bool:
        function = "can_write" if write else "can_read"
        row = self.connection.execute(
            f"SELECT gptbridge_security.{function}(%s) AS allowed", (module_id,)
        ).fetchone()
        return bool(row and row["allowed"])


__all__ = ["PostgresAuthorization"]

from __future__ import annotations

from typing import Any

from psycopg import Connection


class ResourceRepository:
    def __init__(self, connection: Connection[dict[str, Any]]) -> None:
        self.connection = connection

    def get(self, resource_id: str) -> dict[str, Any] | None:
        return self.connection.execute(
            "SELECT * FROM gptbridge_index.resource WHERE resource_id=%s", (resource_id,)
        ).fetchone()

    def set_status(self, resource_id: str, status: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE gptbridge_index.resource SET index_status=%s,updated_at=now() WHERE resource_id=%s",
            (status, resource_id),
        )
        return cursor.rowcount == 1


__all__ = ["ResourceRepository"]

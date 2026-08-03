from __future__ import annotations

from psycopg import Connection, sql


class SchemaManager:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def ensure(self, *names: str) -> None:
        for name in names:
            self.connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(name)))


__all__ = ["SchemaManager"]

from __future__ import annotations

from psycopg import Connection, sql


class IndexManager:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def ensure(self, name: str, schema: str, table: str, columns: tuple[str, ...]) -> None:
        if not columns:
            raise ValueError("INDEX_COLUMNS_REQUIRED")
        self.connection.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
            sql.Identifier(name), sql.Identifier(schema), sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, columns)),
        ))


__all__ = ["IndexManager"]

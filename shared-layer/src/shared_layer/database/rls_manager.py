from __future__ import annotations

from psycopg import Connection, sql


class RlsManager:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def enforce(self, schema: str, table: str) -> None:
        target = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
        self.connection.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(target))
        self.connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(target))


__all__ = ["RlsManager"]

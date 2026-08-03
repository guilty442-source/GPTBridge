from __future__ import annotations

from psycopg import Connection, sql


class RoleManager:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def ensure_group(self, role: str) -> bool:
        if self.connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            return False
        self.connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        return True

    def grant_membership(self, group: str, login_role: str) -> None:
        self.connection.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(login_role)))


__all__ = ["RoleManager"]

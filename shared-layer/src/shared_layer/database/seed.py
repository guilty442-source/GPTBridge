from __future__ import annotations

from dataclasses import dataclass

from psycopg import Connection


@dataclass(frozen=True)
class ModuleSeed:
    module_id: str
    role_name: str


class SeedRunner:
    """Idempotently seed module ownership scopes; grants remain default-deny."""

    def run(self, connection: Connection, modules: tuple[ModuleSeed, ...]) -> None:
        for module in modules:
            connection.execute(
                """INSERT INTO gptbridge_security.principal(role_name,module_id)
                   VALUES (%s,%s) ON CONFLICT(role_name) DO UPDATE SET module_id=excluded.module_id""",
                (module.role_name, module.module_id),
            )
            connection.execute(
                """INSERT INTO gptbridge_security.principal_scope(role_name,module_id,can_read,can_write)
                   VALUES (%s,%s,true,true) ON CONFLICT(role_name,module_id)
                   DO UPDATE SET can_read=true,can_write=true""",
                (module.role_name, module.module_id),
            )


__all__ = ["ModuleSeed", "SeedRunner"]

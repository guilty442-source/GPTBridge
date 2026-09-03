from __future__ import annotations

"""provision_postgresql_architecture — codex-governed PostgreSQL provisioning.

Integrates the existing PostgreSQL SQL architecture (central_index.sql,
module_private_template.sql, xingcheng cognition/identity schemas and the new
identity_directory.sql) under Governance Codex A8/A37/A44/A49 + E21/E23/E30/E35.

PostgreSQL, Qdrant, Python, C++, RAG and git are declared formal tools; the
implementation language remains Python/C++ (A35/E21).
"""

import json
import os
import re
from pathlib import Path

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[2]
CENTRAL_SCHEMA = ROOT / "shared-layer" / "sql" / "central_index.sql"
REGISTRY_MIGRATION = ROOT / "shared-layer" / "migrations" / "001_registry_locations.sql"
IDENTITY_SCHEMA = ROOT / "shared-layer" / "sql" / "identity_directory.sql"
MODULE_SCHEMA = ROOT / "shared-layer" / "sql" / "module_private_template.sql"
XINGCHENG_COGNITION_SCHEMA = ROOT / "local-model" / "xingcheng" / "databases" / "cognition.sql"
XINGCHENG_IDENTITY_SCHEMAS = sorted(
    (ROOT / "local-model" / "xingcheng" / "databases" / "identity").glob("*.sql")
)

CENTRAL_DATABASE = "gptbridge"


def _admin_dsn() -> str:
    value = str(os.environ.get("GPTBRIDGE_POSTGRES_ADMIN_DSN") or "").strip()
    if not value:
        raise RuntimeError("GPTBRIDGE_POSTGRES_ADMIN_DSN_REQUIRED")
    return value


def _module_ids() -> tuple[str, ...]:
    modules: list[str] = []
    for manifest in ROOT.glob("*/manifest.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        module_id = str(payload.get("id") or "").strip().casefold()
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", module_id):
            modules.append(module_id)
    return tuple(sorted(set(modules)))


def _role(module_id: str, suffix: str) -> str:
    return "gptbridge_module_" + module_id.replace("-", "_") + "_" + suffix


def _ensure_database(admin_dsn: str, database: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (database,)
        ).fetchone()
        if not exists:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )


def _ensure_group_role(connection: psycopg.Connection[object], role_name: str) -> None:
    exists = connection.execute(
        "SELECT 1 FROM pg_roles WHERE rolname=%s", (role_name,)
    ).fetchone()
    if not exists:
        connection.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role_name))
        )


def _provision_central_index(admin_dsn: str) -> None:
    _ensure_database(admin_dsn, CENTRAL_DATABASE)
    central_dsn = _dsn_with_db(admin_dsn, CENTRAL_DATABASE)
    with psycopg.connect(central_dsn, autocommit=True) as connection:
        for schema_path in (CENTRAL_SCHEMA, REGISTRY_MIGRATION, IDENTITY_SCHEMA):
            connection.execute(schema_path.read_text(encoding="utf-8"))


def _provision_module_database(admin_dsn: str, module_id: str, central_dsn: str) -> None:
    reader = _role(module_id, "reader")
    executor = _role(module_id, "executor")
    database = f"gptbridge_module_{module_id.replace('-', '_')}"

    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        _ensure_group_role(connection, reader)
        _ensure_group_role(connection, executor)

    _ensure_database(admin_dsn, database)
    module_dsn = _dsn_with_db(admin_dsn, database)

    with psycopg.connect(module_dsn, autocommit=True) as connection:
        connection.execute(MODULE_SCHEMA.read_text(encoding="utf-8"))
        for schema_name in ("module_data", "module_state", "module_audit"):
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(
                    sql.Identifier(schema_name),
                    sql.Identifier(reader),
                    sql.Identifier(executor),
                )
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                    sql.Identifier(schema_name), sql.Identifier(reader)
                )
            )
            connection.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}"
                ).format(sql.Identifier(schema_name), sql.Identifier(executor))
            )

        policies = (
            ("module_data", "resource", "module_id"),
            ("module_data", "locator_map", "module_id"),
            ("module_state", "operation", "module_id"),
            ("module_audit", "event", "module_id"),
        )
        for schema_name, table_name, module_column in policies:
            read_policy = f"{table_name}_owner_read"
            write_policy = f"{table_name}_owner_write"
            connection.execute(
                sql.SQL("DROP POLICY IF EXISTS {} ON {}.{}").format(
                    sql.Identifier(read_policy),
                    sql.Identifier(schema_name),
                    sql.Identifier(table_name),
                )
            )
            connection.execute(
                sql.SQL("DROP POLICY IF EXISTS {} ON {}.{}").format(
                    sql.Identifier(write_policy),
                    sql.Identifier(schema_name),
                    sql.Identifier(table_name),
                )
            )
            connection.execute(
                sql.SQL(
                    "CREATE POLICY {} ON {}.{} FOR SELECT USING ({} = {} AND (pg_has_role(current_user, {}, 'member') OR pg_has_role(current_user, {}, 'member')))"
                ).format(
                    sql.Identifier(read_policy),
                    sql.Identifier(schema_name),
                    sql.Identifier(table_name),
                    sql.Identifier(module_column),
                    sql.Literal(module_id),
                    sql.Literal(reader),
                    sql.Literal(executor),
                )
            )
            connection.execute(
                sql.SQL(
                    "CREATE POLICY {} ON {}.{} FOR ALL USING ({} = {} AND pg_has_role(current_user, {}, 'member')) WITH CHECK ({} = {} AND pg_has_role(current_user, {}, 'member'))"
                ).format(
                    sql.Identifier(write_policy),
                    sql.Identifier(schema_name),
                    sql.Identifier(table_name),
                    sql.Identifier(module_column),
                    sql.Literal(module_id),
                    sql.Literal(executor),
                    sql.Identifier(module_column),
                    sql.Literal(module_id),
                    sql.Literal(executor),
                )
            )

        if module_id == "xingcheng":
            if XINGCHENG_COGNITION_SCHEMA.is_file():
                connection.execute(XINGCHENG_COGNITION_SCHEMA.read_text(encoding="utf-8"))
            for identity_schema in XINGCHENG_IDENTITY_SCHEMAS:
                connection.execute(identity_schema.read_text(encoding="utf-8"))

    with psycopg.connect(central_dsn, autocommit=True) as connection:
        for role_name, scope_name in ((reader, "read"), (executor, "write")):
            can_read = scope_name == "read"
            can_write = scope_name == "write"
            connection.execute(
                """
                INSERT INTO gptbridge_security.principal (role_name, module_id, global_read, transport_execute, audit_write)
                VALUES (%s, %s, %s, false, false)
                ON CONFLICT (role_name) DO UPDATE
                SET module_id = EXCLUDED.module_id, global_read = EXCLUDED.global_read
                """,
                (role_name, module_id, can_read),
            )
            connection.execute(
                """
                INSERT INTO gptbridge_security.principal_scope (role_name, module_id, can_read, can_write)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (role_name, module_id) DO UPDATE
                SET can_read = EXCLUDED.can_read, can_write = EXCLUDED.can_write
                """,
                (role_name, module_id, can_read, can_write),
            )


def _dsn_with_db(dsn: str, database: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    values = conninfo_to_dict(dsn)
    values["dbname"] = database
    return make_conninfo(**values)


def main() -> int:
    admin_dsn = _admin_dsn()
    modules = _module_ids()

    _provision_central_index(admin_dsn)
    central_dsn = _dsn_with_db(admin_dsn, CENTRAL_DATABASE)

    for module_id in modules:
        _provision_module_database(admin_dsn, module_id, central_dsn)

    print(
        json.dumps(
            {
                "ok": True,
                "engine": "postgresql",
                "central_database": CENTRAL_DATABASE,
                "modules": modules,
                "schemas_applied": [
                    str(CENTRAL_SCHEMA.name),
                    str(REGISTRY_MIGRATION.name),
                    str(IDENTITY_SCHEMA.name),
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

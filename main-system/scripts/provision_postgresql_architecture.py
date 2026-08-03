from __future__ import annotations

import json
import os
import re
import hashlib
from datetime import date, datetime
from pathlib import Path

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[2]
CENTRAL_SCHEMA = ROOT / "shared-layer" / "sql" / "central_index.sql"
MODULE_SCHEMA = ROOT / "shared-layer" / "sql" / "module_private_template.sql"
IDENTITY_SCHEMA_DIRECTORY = ROOT / "local-model" / "local-ai" / "databases" / "identity"
COGNITION_SCHEMA = ROOT / "local-model" / "local-ai" / "databases" / "cognition.sql"


def _dsn() -> str:
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


def _ensure_group_role(connection: psycopg.Connection[object], role_name: str) -> None:
    exists = connection.execute(
        "SELECT 1 FROM pg_roles WHERE rolname=%s", (role_name,)
    ).fetchone()
    if not exists:
        connection.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role_name))
        )


def _required_dsn(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name}_REQUIRED")
    return value


def _execute_sql_modules(
    connection: psycopg.Connection[object], directory: Path
) -> tuple[str, ...]:
    modules = tuple(sorted(directory.glob("*.sql")))
    if not modules:
        raise RuntimeError(f"SQL_MODULES_REQUIRED:{directory}")
    for module in modules:
        connection.execute(module.read_text(encoding="utf-8"))
    return tuple(module.name for module in modules)


def _provision_module_database(module_id: str, dsn: str) -> None:
    reader = _role(module_id, "reader")
    executor = _role(module_id, "executor")
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(MODULE_SCHEMA.read_text(encoding="utf-8"))
        for schema_name in ("module_data", "module_state", "module_audit"):
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {},{}").format(
                    sql.Identifier(schema_name), sql.Identifier(reader),
                    sql.Identifier(executor),
                )
            )
        connection.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA module_data,module_state,module_audit TO {}").format(sql.Identifier(reader)))
        connection.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA module_data,module_state,module_audit TO {}").format(sql.Identifier(executor)))
        policies = (
            ("module_data", "resource", "module_id"),
            ("module_data", "locator_map", "module_id"),
            ("module_state", "operation", "module_id"),
            ("module_audit", "event", "module_id"),
        )
        for schema_name, table_name, module_column in policies:
            read_policy = f"{table_name}_owner_read"
            write_policy = f"{table_name}_owner_write"
            connection.execute(sql.SQL("DROP POLICY IF EXISTS {} ON {}.{}").format(sql.Identifier(read_policy), sql.Identifier(schema_name), sql.Identifier(table_name)))
            connection.execute(sql.SQL("DROP POLICY IF EXISTS {} ON {}.{}").format(sql.Identifier(write_policy), sql.Identifier(schema_name), sql.Identifier(table_name)))
            connection.execute(
                sql.SQL("CREATE POLICY {} ON {}.{} FOR SELECT USING ({}={} AND (pg_has_role(current_user,{},'member') OR pg_has_role(current_user,{},'member')))").format(
                    sql.Identifier(read_policy), sql.Identifier(schema_name),
                    sql.Identifier(table_name), sql.Identifier(module_column),
                    sql.Literal(module_id), sql.Literal(reader), sql.Literal(executor),
                )
            )
            connection.execute(
                sql.SQL("CREATE POLICY {} ON {}.{} FOR ALL USING ({}={} AND pg_has_role(current_user,{},'member')) WITH CHECK ({}={} AND pg_has_role(current_user,{},'member'))").format(
                    sql.Identifier(write_policy), sql.Identifier(schema_name),
                    sql.Identifier(table_name), sql.Identifier(module_column),
                    sql.Literal(module_id), sql.Literal(executor),
                    sql.Identifier(module_column), sql.Literal(module_id),
                    sql.Literal(executor),
                )
            )


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _integrate_legacy_xingcheng_model_data(
    identity_dsn: str, cognition_dsn: str
) -> int:
    """Move legacy role profiles into model data before enforcing empty identity."""
    with psycopg.connect(identity_dsn) as identity:
        exists = identity.execute(
            "SELECT to_regclass('identity.role_profile') IS NOT NULL"
        ).fetchone()
        if not exists or not bool(exists[0]):
            return 0
        profiles = identity.execute(
            """
            SELECT resource_id,platform_id,module_id,owner_id,resource_label,
                   profile_type,value,version,content_hash,created_at,updated_at
            FROM identity.role_profile ORDER BY resource_id
            """
        ).fetchall()
        resources: list[tuple[object, ...]] = []
        for row in profiles:
            versions = identity.execute(
                """
                SELECT version,value,content_hash,created_at
                FROM identity.role_version
                WHERE resource_id=%s ORDER BY version
                """,
                (row[0],),
            ).fetchall()
            payload = {
                "legacy_profile": {
                    "profile_type": row[5],
                    "value": row[6],
                    "version": row[7],
                    "created_at": row[9],
                    "updated_at": row[10],
                },
                "legacy_versions": [
                    {
                        "version": version[0],
                        "value": version[1],
                        "content_hash": version[2],
                        "created_at": version[3],
                    }
                    for version in versions
                ],
            }
            serialized = json.dumps(
                payload, ensure_ascii=False, default=_json_default, sort_keys=True
            )
            resources.append(
                (
                    f"legacy-role-profile:{row[0]}", row[1], row[2], row[3],
                    f"舊模型資料：{row[4]} [{row[0]}]", "legacy-role-profile",
                    serialized, row[7],
                    hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                    row[9], row[10],
                )
            )

        with psycopg.connect(cognition_dsn) as cognition:
            for resource in resources:
                cognition.execute(
                    """
                    INSERT INTO cognition.model_data
                        (resource_id,platform_id,module_id,owner_id,resource_label,
                         data_type,value,version,content_hash,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
                    ON CONFLICT (resource_id) DO UPDATE SET
                        value=EXCLUDED.value,version=EXCLUDED.version,
                        content_hash=EXCLUDED.content_hash,
                        updated_at=EXCLUDED.updated_at
                    """,
                    resource,
                )
        identity.execute("DROP TABLE IF EXISTS identity.role_version")
        identity.execute("DROP TABLE IF EXISTS identity.role_profile")
        identity.execute("DROP SCHEMA IF EXISTS identity")
        return len(resources)
def main() -> int:
    if not CENTRAL_SCHEMA.is_file():
        raise RuntimeError("CENTRAL_INDEX_SCHEMA_REQUIRED")
    modules = _module_ids()
    module_dsns = json.loads(_required_dsn("GPTBRIDGE_MODULE_DSNS"))
    if not isinstance(module_dsns, dict):
        raise RuntimeError("GPTBRIDGE_MODULE_DSNS_INVALID")
    with psycopg.connect(_dsn(), autocommit=True) as connection:
        connection.execute(CENTRAL_SCHEMA.read_text(encoding="utf-8"))
        for module_id in modules:
            reader = _role(module_id, "reader")
            executor = _role(module_id, "executor")
            _ensure_group_role(connection, reader)
            _ensure_group_role(connection, executor)
            connection.execute(
                sql.SQL("GRANT gptbridge_index_reader TO {}").format(
                    sql.Identifier(reader)
                )
            )
            connection.execute(
                sql.SQL("GRANT gptbridge_index_executor TO {}").format(
                    sql.Identifier(executor)
                )
            )
            connection.execute(
                """
                INSERT INTO gptbridge_security.principal (role_name,module_id)
                VALUES (%s,%s) ON CONFLICT (role_name)
                DO UPDATE SET module_id=EXCLUDED.module_id,global_read=false
                """,
                (reader, module_id),
            )
            connection.execute(
                """
                INSERT INTO gptbridge_security.principal (role_name,module_id)
                VALUES (%s,%s) ON CONFLICT (role_name)
                DO UPDATE SET module_id=EXCLUDED.module_id,global_read=false
                """,
                (executor, module_id),
            )
            connection.execute(
                """
                INSERT INTO gptbridge_security.principal_scope
                    (role_name,module_id,can_read,can_write)
                VALUES (%s,%s,true,false) ON CONFLICT (role_name,module_id)
                DO UPDATE SET can_read=true,can_write=false
                """,
                (reader, module_id),
            )
            connection.execute(
                """
                INSERT INTO gptbridge_security.principal_scope
                    (role_name,module_id,can_read,can_write)
                VALUES (%s,%s,true,true) ON CONFLICT (role_name,module_id)
                DO UPDATE SET can_read=true,can_write=true
                """,
                (executor, module_id),
            )
    missing = [module_id for module_id in modules if not str(module_dsns.get(module_id) or "").strip()]
    if missing:
        raise RuntimeError("MODULE_DSNS_REQUIRED:" + ",".join(missing))
    for module_id in modules:
        _provision_module_database(module_id, str(module_dsns[module_id]))
    identity_dsn = _required_dsn("GPTBRIDGE_XINGCHENG_IDENTITY_DSN")
    cognition_dsn = _required_dsn("GPTBRIDGE_XINGCHENG_COGNITION_DSN")
    with psycopg.connect(cognition_dsn, autocommit=True) as connection:
        connection.execute(COGNITION_SCHEMA.read_text(encoding="utf-8"))
    integrated = _integrate_legacy_xingcheng_model_data(identity_dsn, cognition_dsn)
    with psycopg.connect(identity_dsn, autocommit=True) as connection:
        identity_modules = _execute_sql_modules(connection, IDENTITY_SCHEMA_DIRECTORY)
    print(json.dumps({
        "ok": True,
        "modules": modules,
        "default_cross_module": "deny",
        "legacy_model_records_integrated": integrated,
        "xingcheng_role_setting_records": 0,
        "xingcheng_identity_modules": identity_modules,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

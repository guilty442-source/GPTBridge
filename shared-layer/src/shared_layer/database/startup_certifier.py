"""Startup Certifier and Read-Only Domain (migration 030 + D6).

Startup Certification: at startup, verify schema version, RLS, required
roles, migration head, audit append-only, and authority contract — not
just SELECT 1 — before marking the database READY.

Read-Only Domain: when integrity/schema drift/authority conflict occurs,
a specific data domain can be switched to read-only.

Usage:
    from shared_layer.database.startup_certifier import certify_startup, is_ready
    from shared_layer.database.readonly_domain import set_readonly, is_readonly

    with connection_manager.connection() as conn:
        result = certify_startup(conn, certified_by="startup-gate")
        if not result["ready"]:
            # Switch affected domain to read-only
            set_readonly(conn, domain="central-index", reason="schema-drift")

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist; deny-by-default.
    A46/E22 — Audit: mandatory-ledger.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from psycopg import Connection

_RECORD_STARTUP = (
    "SELECT gptbridge_index.record_startup_certification("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_IS_READY = "SELECT gptbridge_index.is_database_ready()"
_SET_READONLY = "SELECT gptbridge_index.set_domain_readonly(%s, %s, %s, %s)"
_IS_READONLY = "SELECT gptbridge_index.is_domain_readonly(%s)"


def certify_startup(
    connection: Connection[Any],
    *,
    certified_by: str,
) -> dict[str, Any]:
    """Run startup certification checks and record the result.

    Returns a dict with 'ready' (bool) and 'certification_id' (UUID or None).
    """
    import json

    checks: list[dict[str, Any]] = []

    # 1. Schema version verified
    schema_ok = False
    try:
        row = connection.execute(
            "SELECT count(*) FROM gptbridge_migration.history"
        ).fetchone()
        schema_ok = bool(row and int(row[0]) > 0)
    except Exception:
        pass
    checks.append({"name": "schema_version", "passed": schema_ok})

    # 2. RLS verified
    rls_ok = False
    try:
        row = connection.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname LIKE 'gptbridge_%' "
            "AND rowsecurity = true"
        ).fetchone()
        rls_ok = bool(row and int(row[0]) > 0)
    except Exception:
        pass
    checks.append({"name": "rls", "passed": rls_ok})

    # 3. Required roles verified
    roles_ok = False
    try:
        row = connection.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'gptbridge_%'"
        ).fetchone()
        roles_ok = bool(row and int(row[0]) >= 4)
    except Exception:
        pass
    checks.append({"name": "required_roles", "passed": roles_ok})

    # 4. Migration head verified
    migration_ok = False
    try:
        row = connection.execute(
            "SELECT count(*) FROM gptbridge_migration.history "
            "WHERE migration_id >= 30"
        ).fetchone()
        migration_ok = bool(row and int(row[0]) > 0)
    except Exception:
        pass
    checks.append({"name": "migration_head", "passed": migration_ok})

    # 5. Audit append-only verified
    audit_ok = False
    try:
        row = connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'audit_append_only'"
        ).fetchone()
        audit_ok = bool(row and int(row[0]) > 0)
    except Exception:
        pass
    checks.append({"name": "audit_append_only", "passed": audit_ok})

    # 6. Authority contract verified
    authority_ok = False
    try:
        row = connection.execute(
            "SELECT count(*) FROM gptbridge_index.contract_version"
        ).fetchone()
        authority_ok = bool(row and int(row[0]) > 0)
    except Exception:
        pass
    checks.append({"name": "authority_contract", "passed": authority_ok})

    # 7. Contract version verified
    contract_ok = False
    try:
        row = connection.execute(
            "SELECT count(*) FROM gptbridge_index.contract_version "
            "WHERE contract_name = 'central-index'"
        ).fetchone()
        contract_ok = bool(row and int(row[0]) > 0)
    except Exception:
        pass
    checks.append({"name": "contract_version", "passed": contract_ok})

    row = connection.execute(
        _RECORD_STARTUP,
        (schema_ok, rls_ok, roles_ok, migration_ok, audit_ok,
         authority_ok, contract_ok, json.dumps(checks), certified_by),
    ).fetchone()
    cert_id = UUID(str(row[0])) if row and row[0] else None
    ready = all(c["passed"] for c in checks)
    return {"ready": ready, "certification_id": cert_id, "checks": checks}


def is_ready(connection: Connection[Any]) -> bool:
    """Check if the latest startup certification passed."""
    row = connection.execute(_IS_READY).fetchone()
    return bool(row and row[0])


__all__ = ["certify_startup", "is_ready"]

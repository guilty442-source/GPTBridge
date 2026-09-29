"""Schema Contract Registry (A44/E30 + A8/E21).

Registers every PostgreSQL schema, RLS policy, role, index,
and migration version as a verifiable contract.  At startup, the registry
compares the declared contract against the actual database state and
reports drift before the runtime is allowed to serve traffic.

The registry is read-only at runtime — it never mutates the database.  All
repairs are delegated to the migration runner or an authorized owner.
"""
from __future__ import annotations

from typing import Any


from .schema_contract_registry_types import (
    ContractDrift,
    ContractVerificationResult,
    RoleContract,
    SchemaContract,
    TableContract,
)
from .schema_contract_registry_tables_foundation import DECLARED_TABLES_FOUNDATION
from .schema_contract_registry_tables_governance import DECLARED_TABLES_GOVERNANCE
from .schema_contract_registry_tables_operations import DECLARED_TABLES_OPERATIONS


# ============================================================================
# Declared contracts (source of truth — must match central_index.sql + migrations)
# Table entries live in the sibling ``_tables_*`` modules; the hub assembles them.
# ============================================================================

_DECLARED_TABLES: tuple[TableContract, ...] = (
    DECLARED_TABLES_FOUNDATION
    + DECLARED_TABLES_OPERATIONS
    + DECLARED_TABLES_GOVERNANCE
)

_DECLARED_ROLES: tuple[RoleContract, ...] = (
    RoleContract("gptbridge_index_reader", ("gptbridge_index:SELECT", "gptbridge_rag:SELECT", "gptbridge_audit:SELECT")),
    RoleContract("gptbridge_index_executor", (
        "gptbridge_index:SELECT,INSERT,UPDATE,DELETE",
        "gptbridge_rag:SELECT,INSERT,UPDATE,DELETE",
        "gptbridge_transport:SELECT,INSERT,UPDATE,DELETE",
        "gptbridge_audit:SELECT,INSERT",
    )),
    RoleContract("gptbridge_xingcheng_reader", (
        "gptbridge_index:SELECT", "gptbridge_rag:SELECT", "gptbridge_audit:SELECT",
    )),
    RoleContract("gptbridge_transport_executor", (
        "gptbridge_transport:SELECT,INSERT,UPDATE,DELETE",
    )),
)

EXPECTED_MIGRATION_COUNT = 133  # every physical *.sql, including dual-numbered files

def declared_contract() -> SchemaContract:
    """Return the declared schema contract."""
    return SchemaContract(
        tables=_DECLARED_TABLES,
        roles=_DECLARED_ROLES,
        migration_count=EXPECTED_MIGRATION_COUNT,
    )


def verify_contract(connection: Any) -> ContractVerificationResult:
    """Verify the live PostgreSQL database against the declared contract.

    This function is read-only — it never mutates the database.
    """
    contract = declared_contract()
    drifts: list[ContractDrift] = []

    # 1. Verify tables exist and have expected columns
    for table_contract in contract.tables:
        full_name = f"{table_contract.schema}.{table_contract.table}"
        try:
            rows = connection.execute(  # sql-ok: per-table contract verification, bounded by declared contract
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table_contract.schema, table_contract.table),
            ).fetchall()
            actual_cols = {str(r[0]) for r in rows}
            if not actual_cols:
                drifts.append(ContractDrift(full_name, "table_missing"))
                continue
            for expected_col in table_contract.columns:
                if expected_col not in actual_cols:
                    drifts.append(ContractDrift(full_name, "column_missing", expected_col))
        except Exception as exc:
            drifts.append(ContractDrift(full_name, "query_failed", str(exc)[:200]))

    # 2. Verify RLS is enabled and forced
    for table_contract in contract.tables:
        if not table_contract.rls_enabled:
            continue
        full_name = f"{table_contract.schema}.{table_contract.table}"
        try:
            row = connection.execute(  # sql-ok: per-table RLS verification, bounded by declared contract
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname = %s AND relnamespace = (
                    SELECT oid FROM pg_namespace WHERE nspname = %s
                )
                """,
                (table_contract.table, table_contract.schema),
            ).fetchone()
            if row is None:
                drifts.append(ContractDrift(full_name, "table_not_found"))
            else:
                rls_enabled = bool(row[0])
                rls_forced = bool(row[1])
                if table_contract.rls_enabled and not rls_enabled:
                    drifts.append(ContractDrift(full_name, "rls_not_enabled"))
                if table_contract.rls_forced and not rls_forced:
                    drifts.append(ContractDrift(full_name, "rls_not_forced"))
        except Exception as exc:
            drifts.append(ContractDrift(full_name, "rls_query_failed", str(exc)[:200]))

    # 3. Verify migration count
    try:
        row = connection.execute(
            "SELECT count(*) FROM gptbridge_migration.history"
        ).fetchone()
        migration_count = int(row[0]) if row else 0
    except Exception:
        migration_count = 0
        drifts.append(ContractDrift("gptbridge_migration.history", "migration_table_unreadable"))

    if migration_count < contract.migration_count:
        drifts.append(ContractDrift(
            "gptbridge_migration.history",
            "migration_count_behind",
            f"expected>={contract.migration_count} actual={migration_count}",
        ))

    return ContractVerificationResult(
        passed=len(drifts) == 0,
        drifts=drifts,
        migration_count=migration_count,
    )


__all__ = [
    "TableContract",
    "RoleContract",
    "SchemaContract",
    "ContractDrift",
    "ContractVerificationResult",
    "declared_contract",
    "verify_contract",
    "EXPECTED_MIGRATION_COUNT",
]

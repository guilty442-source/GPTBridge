"""Write Provenance Helper (migration 018–020 + A5).

Sets session variables on a PostgreSQL connection so triggers can
auto-populate lineage, authority, and provenance columns on every
INSERT/UPDATE.  This is the single entry point for runtime code to
declare "who is writing, why, and from what revision" before a write.

Session variables set (all read via ``current_setting('gptbridge.*', true)``):
    gptbridge.actor_id          — the human/agent initiating the action
    gptbridge.executor_id       — the governed executor performing the write
    gptbridge.decision_id       — the governance decision authorising the write
    gptbridge.correlation_id    — cross-engine correlation id for tracing
    gptbridge.source_revision   — the revision this write is based on
    gptbridge.connection_generation — generation fence (migration 016)
    gptbridge.contract_version  — schema contract version handshake (Phase B)

Usage:
    from shared_layer.database.provenance import set_provenance

    with connection_manager.connection() as conn:
        set_provenance(
            conn,
            actor_id="user-abc",
            executor_id="gptbridge_index_executor",
            decision_id="DEC-123",
            correlation_id="COR-456",
            source_revision=42,
            generation=current_generation,
        )
        conn.execute("INSERT INTO gptbridge_index.resource ...")

Codex basis:
    A46/E22 — Audit: mandatory-ledger; write=governed-executor.
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist; deny-by-default.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg import Connection
from psycopg import sql

# Session variables that triggers read (migration 018/020).
_PROVENANCE_VARS: tuple[str, ...] = (
    "actor_id",
    "executor_id",
    "decision_id",
    "correlation_id",
    "source_revision",
    "connection_generation",
    "contract_version",
)


def set_provenance(
    connection: Connection[Any],
    *,
    actor_id: Optional[str] = None,
    executor_id: Optional[str] = None,
    decision_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    source_revision: Optional[int] = None,
    generation: Optional[int] = None,
    contract_version: Optional[str] = None,
) -> None:
    """Set provenance session variables on a PostgreSQL connection.

    Uses ``SET LOCAL`` so variables are scoped to the current transaction
    and automatically cleared on COMMIT/ROLLBACK.  Call this inside a
    transaction, before the INSERT/UPDATE that should carry provenance.

    ``None`` values are skipped (the trigger will fall back to its own
    default, typically 'unknown' or NULL).
    """
    values: dict[str, str] = {}
    if actor_id is not None:
        values["actor_id"] = actor_id
    if executor_id is not None:
        values["executor_id"] = executor_id
    if decision_id is not None:
        values["decision_id"] = decision_id
    if correlation_id is not None:
        values["correlation_id"] = correlation_id
    if source_revision is not None:
        values["source_revision"] = str(int(source_revision))
    if generation is not None:
        values["connection_generation"] = str(int(generation))
    if contract_version is not None:
        values["contract_version"] = contract_version

    for var, val in values.items():
        statement = sql.SQL("SET LOCAL gptbridge.{var} = {val}").format(
            var=sql.Identifier(var),
            val=sql.Literal(val),
        )
        connection.execute(statement)


def clear_provenance(connection: Connection[Any]) -> None:
    """Reset all provenance session variables to NULL.

    Useful after a write that should not carry provenance into subsequent
    unrelated statements in the same session.
    """
    for var in _PROVENANCE_VARS:
        statement = sql.SQL("SET LOCAL gptbridge.{var} = NULL").format(
            var=sql.Identifier(var),
        )
        connection.execute(statement)


def set_contract_version(
    connection: Connection[Any],
    contract_version: str,
) -> None:
    """Declare the contract version for this connection's session.

    Must be called before any write to a governed table.  The contract
    version fence trigger (migration 024) rejects writes from sessions
    that declared an incompatible version.

    Uses ``SET`` (session-level, not transaction-scoped) so the version
    persists across transactions in the same connection.
    """
    statement = sql.SQL("SET gptbridge.contract_version = {val}").format(
        val=sql.Literal(contract_version),
    )
    connection.execute(statement)


def set_migration_executor(connection: Connection[Any]) -> None:
    """Mark this connection as authorized to run DDL.

    Must be called before running migrations.  The DDL guard event trigger
    (migration 022) rejects DDL from sessions that did not set this flag.
    """
    statement = sql.SQL("SET gptbridge.is_migration_executor = {val}").format(
        val=sql.Literal("true"),
    )
    connection.execute(statement)


def clear_migration_executor(connection: Connection[Any]) -> None:
    """Clear the migration executor flag after DDL is complete."""
    statement = sql.SQL("SET gptbridge.is_migration_executor = {val}").format(
        val=sql.Literal("false"),
    )
    connection.execute(statement)


__all__ = [
    "set_provenance",
    "clear_provenance",
    "set_contract_version",
    "set_migration_executor",
    "clear_migration_executor",
]

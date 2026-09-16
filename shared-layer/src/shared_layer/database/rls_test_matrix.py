"""RLS Test Matrix (A10/E10 + A44/E30).

Tests every Role × Table × Operation (SELECT/INSERT/UPDATE/DELETE) to detect
permission drift after migrations.  Each cell is expected to be allow or
deny based on the declared contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Expectation(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class RlsTestCell:
    """One cell in the RLS test matrix."""

    role: str
    schema: str
    table: str
    operation: str  # SELECT, INSERT, UPDATE, DELETE
    expectation: Expectation


@dataclass
class RlsTestResult:
    """Result of running one RLS test cell."""

    cell: RlsTestCell
    passed: bool
    actual: str  # "allow" or "deny"
    error: str = ""


@dataclass
class RlsMatrixResult:
    """Result of running the full RLS test matrix."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    results: list[RlsTestResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


# ============================================================================
# Declared RLS matrix (source of truth)
# Roles × Tables × Operations → allow/deny
# ============================================================================

_ROLES = (
    "gptbridge_index_reader",
    "gptbridge_index_executor",
    "gptbridge_xingcheng_reader",
    "gptbridge_transport_executor",
)

_TABLES = (
    ("gptbridge_index", "resource"),
    ("gptbridge_index", "resource_relation"),
    ("gptbridge_index", "schema_version"),
    ("gptbridge_index", "module_version"),
    ("gptbridge_index", "reconcile_conflict_log"),
    ("gptbridge_index", "retention_policy"),
    ("gptbridge_index", "backup_catalog"),
    ("gptbridge_index", "backend_generation_state"),
    ("gptbridge_index", "maintenance_window"),
    ("gptbridge_rag", "chunk"),
    ("gptbridge_rag", "index_state"),
    ("gptbridge_transport", "tool_request"),
    ("gptbridge_audit", "event"),
)

_OPERATIONS = ("SELECT", "INSERT", "UPDATE", "DELETE")


def _declared_matrix() -> tuple[RlsTestCell, ...]:
    """Build the declared RLS expectation matrix."""
    cells: list[RlsTestCell] = []
    for role in _ROLES:
        for schema, table in _TABLES:
            for op in _OPERATIONS:
                expectation = _expectation_for(role, schema, table, op)
                cells.append(RlsTestCell(role, schema, table, op, expectation))
    return tuple(cells)


def _expectation_for(role: str, schema: str, table: str, op: str) -> Expectation:
    """Determine the expected allow/deny for a role×table×operation."""
    # Reader roles: SELECT only
    if role in ("gptbridge_index_reader", "gptbridge_xingcheng_reader"):
        if op == "SELECT":
            return Expectation.ALLOW
        return Expectation.DENY

    # Transport executor: only transport table
    if role == "gptbridge_transport_executor":
        if schema == "gptbridge_transport" and op in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            return Expectation.ALLOW
        if schema == "gptbridge_audit" and op == "INSERT":
            return Expectation.ALLOW
        return Expectation.DENY

    # Index executor: broad access to index/rag/transport, INSERT on audit
    if role == "gptbridge_index_executor":
        if schema in ("gptbridge_index", "gptbridge_rag", "gptbridge_transport"):
            if op in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                return Expectation.ALLOW
        if schema == "gptbridge_audit" and op in ("SELECT", "INSERT"):
            return Expectation.ALLOW
        return Expectation.DENY

    return Expectation.DENY


def declared_matrix() -> tuple[RlsTestCell, ...]:
    """Return the declared RLS test matrix."""
    return _declared_matrix()


def _test_cell(connection: Any, cell: RlsTestCell) -> RlsTestResult:
    """Test a single RLS cell by attempting the operation and catching the result."""
    full_name = f"{cell.schema}.{cell.table}"
    try:
        # Use SET ROLE to impersonate the role, then try the operation
        # We use a savepoint so we can roll back without aborting the transaction
        connection.execute("SAVEPOINT rls_test")
        connection.execute(f"SET ROLE {cell.role}")
        try:
            if cell.operation == "SELECT":
                connection.execute(f"SELECT 1 FROM {full_name} LIMIT 1")
            elif cell.operation == "INSERT":
                # Try a minimal insert; expect it to fail due to constraints
                # even if RLS allows it — we only care about RLS here
                try:
                    connection.execute(f"INSERT INTO {full_name} DEFAULT VALUES")
                except Exception:
                    pass  # Constraint failure is fine — RLS allowed it
            elif cell.operation == "UPDATE":
                connection.execute(f"UPDATE {full_name} SET updated_at = now() WHERE false")
            elif cell.operation == "DELETE":
                connection.execute(f"DELETE FROM {full_name} WHERE false")
            actual = "allow"
        except Exception:
            actual = "deny"
        connection.execute("ROLLBACK TO SAVEPOINT rls_test")
        connection.execute("SET ROLE NONE")
        passed = (actual == cell.expectation.value)
        return RlsTestResult(cell, passed, actual)
    except Exception as exc:
        try:
            connection.execute("ROLLBACK TO SAVEPOINT rls_test")
            connection.execute("SET ROLE NONE")
        except Exception:
            pass
        return RlsTestResult(cell, False, "error", str(exc)[:200])


def run_matrix(connection: Any) -> RlsMatrixResult:
    """Run the full RLS test matrix against the live database."""
    matrix = _declared_matrix()
    result = RlsMatrixResult(total=len(matrix))
    for cell in matrix:
        cell_result = _test_cell(connection, cell)
        result.results.append(cell_result)
        if cell_result.passed:
            result.passed += 1
        else:
            result.failed += 1
    return result


__all__ = [
    "Expectation",
    "RlsTestCell",
    "RlsTestResult",
    "RlsMatrixResult",
    "declared_matrix",
    "run_matrix",
]

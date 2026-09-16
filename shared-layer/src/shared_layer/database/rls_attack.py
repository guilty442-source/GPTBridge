"""RLS attack matrix (A10, A365 PRIORITY-5, A499).

Extends :mod:`rls_test_matrix` with the scope classification the
penetration test requires: every cell is labelled with *whose* data the
role is reaching for — own module, another module, xingcheng, a
governance-related classification, the audit ledger, or transport.

The decisive property: every cell executes ``SET ROLE`` directly on the
connection, bypassing the Python Access Gateway entirely.  A PASS proves
PostgreSQL RLS itself denies the operation — the gateway is a second
layer, not the enforcement of last resort.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .rls_test_matrix import (
    Expectation,
    RlsTestCell,
    declared_matrix,
    run_matrix,
)


class Scope(str, Enum):
    OWN_MODULE = "own-module"
    OTHER_MODULE = "other-module"
    XINGCHENG = "xingcheng"
    GOVERNANCE = "governance-classification"
    AUDIT = "audit"
    TRANSPORT = "transport"


_SCOPE_BY_TABLE: dict[tuple[str, str], Scope] = {
    ("gptbridge_index", "resource"): Scope.OWN_MODULE,
    ("gptbridge_index", "resource_relation"): Scope.OTHER_MODULE,
    ("gptbridge_index", "schema_version"): Scope.GOVERNANCE,
    ("gptbridge_index", "module_version"): Scope.GOVERNANCE,
    ("gptbridge_index", "reconcile_conflict_log"): Scope.OTHER_MODULE,
    ("gptbridge_index", "retention_policy"): Scope.GOVERNANCE,
    ("gptbridge_index", "backup_catalog"): Scope.GOVERNANCE,
    ("gptbridge_index", "backend_generation_state"): Scope.GOVERNANCE,
    ("gptbridge_index", "maintenance_window"): Scope.GOVERNANCE,
    ("gptbridge_rag", "chunk"): Scope.OWN_MODULE,
    ("gptbridge_rag", "index_state"): Scope.OWN_MODULE,
    ("gptbridge_transport", "tool_request"): Scope.TRANSPORT,
    ("gptbridge_audit", "event"): Scope.AUDIT,
}


@dataclass(frozen=True)
class AttackCell:
    """RLS cell with scope classification."""

    role: str
    scope: Scope
    table: str
    operation: str
    expectation: Expectation
    bypasses_gateway: bool = True  # SET ROLE skips the app layer


def attack_matrix() -> tuple[AttackCell, ...]:
    """Declared attack matrix: role x scope x table x operation."""
    cells: list[AttackCell] = []
    for cell in declared_matrix():
        scope = _SCOPE_BY_TABLE.get(
            (cell.schema, cell.table), Scope.OTHER_MODULE
        )
        # xingcheng scope: reader role reaching for module data
        if cell.role == "gptbridge_xingcheng_reader":
            scope = Scope.XINGCHENG
        cells.append(
            AttackCell(
                role=cell.role,
                scope=scope,
                table=f"{cell.schema}.{cell.table}",
                operation=cell.operation,
                expectation=cell.expectation,
            )
        )
    return tuple(cells)


def coverage_summary() -> dict[str, Any]:
    """Matrix coverage: every role x scope x operation present."""
    cells = attack_matrix()
    roles = sorted({c.role for c in cells})
    scopes = sorted({c.scope.value for c in cells})
    ops = sorted({c.operation for c in cells})
    combos = {
        (c.role, c.scope, c.operation)
        for c in cells
    }
    return {
        "roles": roles,
        "scopes": scopes,
        "operations": ops,
        "cells": len(cells),
        "all_bypass_gateway": all(c.bypasses_gateway for c in cells),
        "role_scope_op_combinations": len(combos),
    }


def run_attack_matrix(connection: Any) -> dict[str, Any]:
    """Run the attack matrix against a live governed connection."""
    result = run_matrix(connection)
    return {
        "total": result.total,
        "passed": result.passed,
        "failed": result.failed,
        "ok": result.ok,
        "coverage": coverage_summary(),
        "failures": [
            {
                "role": r.cell.role,
                "table": f"{r.cell.schema}.{r.cell.table}",
                "operation": r.cell.operation,
                "expected": r.cell.expectation.value,
                "actual": r.actual,
                "error": r.error,
            }
            for r in result.results
            if not r.passed
        ],
    }


__all__ = [
    "AttackCell",
    "Scope",
    "attack_matrix",
    "coverage_summary",
    "run_attack_matrix",
]

"""A334 registry access — 登錄表執行期讀取與驗證（唯讀、fail-closed）。

法典依據 A334:
- ``sovereign_hierarchy_registry`` is the machine authority for every active
  sub-sovereign's single parent and primary domain.
- ``module_assignment_registry`` is the machine authority for every
  executable module's managing sub-sovereign, decision authority,
  permission authority, review authority and exact execution identity.
- ``supersession_registry`` records retired-identity supersession.

Every sovereign identity must resolve to exactly one current formal
identity; every sub-sovereign has exactly one parent; decision, permission,
review and execution stay separated.

This module is read-only and pure: it opens the sealed codex SQLite
database in read-only mode, reads the authoritative ``*_registry`` tables
directly (without modifying ``governance_rule`` loaders), and exposes
validation helpers used by the governed executors (e.g. the sovereign-stack
executor) to enforce the declared hierarchy at materialization and dispatch
time.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from governance_rule.execution.codex_repository import CODEX_DATABASE_PATH


@lru_cache(maxsize=1)
def _registries() -> dict[str, tuple[dict[str, str], ...]]:
    """Load every ``*_registry`` table from the codex DB, read-only."""
    if not Path(CODEX_DATABASE_PATH).exists():
        return {}
    uri = f"file:{Path(CODEX_DATABASE_PATH).as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name LIKE '%_registry' ORDER BY name"
            )
        ]
        loaded: dict[str, tuple[dict[str, str], ...]] = {}
        for table in tables:
            columns = [
                column[1]
                for column in connection.execute(f"PRAGMA table_info({table})")
            ]
            rows = tuple(
                dict(zip(columns, row))
                for row in connection.execute(
                    f"SELECT {', '.join(columns)} FROM {table} ORDER BY 1"
                )
            )
            loaded[table] = tuple(
                {key: str(value) for key, value in row.items()} for row in rows
            )
        return loaded
    finally:
        connection.close()


def _registry(name: str) -> tuple[dict[str, str], ...]:
    return _registries().get(name, ())


def sovereign_hierarchy_registry() -> tuple[dict[str, str], ...]:
    """All active sub-sovereign parent/domain assignments (A334)."""
    return _registry("sovereign_hierarchy_registry")


def module_assignment_registry() -> tuple[dict[str, str], ...]:
    """All executable-module managing/authority assignments (A334)."""
    return _registry("module_assignment_registry")


def supersession_registry() -> tuple[dict[str, str], ...]:
    """Retired-identity supersession records."""
    return _registry("supersession_registry")


def parent_of(child_identity: str) -> str | None:
    """Return the codex-registered single parent of a sub-sovereign."""
    for row in sovereign_hierarchy_registry():
        if row.get("child_identity") == child_identity and row.get("status") == "active":
            return row.get("parent_identity")
    return None


def primary_domain_of(child_identity: str) -> str | None:
    for row in sovereign_hierarchy_registry():
        if row.get("child_identity") == child_identity and row.get("status") == "active":
            return row.get("primary_domain")
    return None


def children_of(parent_identity: str) -> list[str]:
    """Active child identities registered under a parent sovereign."""
    return [
        row["child_identity"]
        for row in sovereign_hierarchy_registry()
        if row.get("parent_identity") == parent_identity
        and row.get("status") == "active"
    ]


def validate_child_parent(child_identity: str, parent_identity: str) -> bool:
    """A334 enforcement: the sub-sovereign has exactly one codex parent."""
    return parent_of(child_identity) == parent_identity


def module_assignment(module_architecture_code: str) -> dict[str, str] | None:
    """Resolve an executable module's registered assignment entry."""
    for row in module_assignment_registry():
        if (
            row.get("module_architecture_code") == module_architecture_code
            and row.get("status") == "active"
        ):
            return row
    return None


def validate_execution_identity(
    module_architecture_code: str, execution_identity: str
) -> bool:
    """A334: execution_identity must equal the registered module code."""
    row = module_assignment(module_architecture_code)
    return row is not None and row.get("execution_identity") == execution_identity


def successor_of(predecessor_identity: str) -> str | None:
    """Resolve a retired identity to its current superseding identity."""
    for row in supersession_registry():
        if (
            row.get("predecessor_identity") == predecessor_identity
            and row.get("status") == "active"
        ):
            return row.get("successor_identity")
    return None


def hierarchy_status() -> dict[str, Any]:
    """Read-only registry surface for status/reporting."""
    hierarchy = sovereign_hierarchy_registry()
    assignments = module_assignment_registry()
    supersessions = supersession_registry()
    return {
        "authority": "codex-A334",
        "hierarchy_entries": len(hierarchy),
        "module_assignments": len(assignments),
        "supersession_entries": len(supersessions),
        "parents": sorted({row["parent_identity"] for row in hierarchy}),
    }


__all__ = [
    "children_of",
    "hierarchy_status",
    "module_assignment",
    "module_assignment_registry",
    "parent_of",
    "primary_domain_of",
    "sovereign_hierarchy_registry",
    "successor_of",
    "supersession_registry",
    "validate_child_parent",
    "validate_execution_identity",
]

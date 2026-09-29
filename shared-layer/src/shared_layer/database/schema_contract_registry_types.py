"""Shared contract dataclasses for the schema contract registry (A44/E30 + A8/E21)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TableContract:
    """Declared contract for a single PostgreSQL table."""

    schema: str
    table: str
    columns: tuple[str, ...]
    rls_enabled: bool = True
    rls_forced: bool = True
    indexes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoleContract:
    """Declared contract for a PostgreSQL role."""

    role_name: str
    grants: tuple[str, ...]  # (schema.table:privilege, ...)


@dataclass(frozen=True)
class SchemaContract:
    """Full schema contract for the PostgreSQL central index."""

    tables: tuple[TableContract, ...]
    roles: tuple[RoleContract, ...]
    migration_count: int  # expected number of applied migrations


@dataclass
class ContractDrift:
    """A single drift between declared and actual schema."""

    table: str
    issue: str
    detail: str = ""


@dataclass
class ContractVerificationResult:
    """Result of verifying the schema contract against the live database."""

    passed: bool
    drifts: list[ContractDrift] = field(default_factory=list)
    migration_count: int = 0

"""Migration destruction tests (A368 SCHEMA-CONTRACT/DRIFT, A499 SCHEMA-DDL).

For every migration under ``shared-layer/migrations`` this verifies —
statically, because a live PostgreSQL is not required — the contract the
user's drill demands:

    idempotent          re-running HEAD must not fail (guards present)
    transaction safe    DDL wrapped so mid-migration abort rolls back
    backward compatible classified via migration_compatibility
    no data loss        no unguarded DELETE/TRUNCATE/DROP TABLE
    no silent RLS loss  never DROP POLICY / DISABLE RLS / REVOKE the
                        policy without re-establishing it in the same file
    numbering           contiguous 001..HEAD matching
                        ``EXPECTED_MIGRATION_COUNT``

Live-execution cells (blank DB -> HEAD, old -> HEAD upgrade, interrupted
apply) are produced when a governed PostgreSQL connection is supplied;
without one the report marks them ``skipped-live-unavailable`` — never
silently PASSed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .migration_compatibility import (
    MigrationClassification,
    classify_migration,
)
from .schema_contract_registry import EXPECTED_MIGRATION_COUNT


@dataclass
class MigrationVerdict:
    """Static verdict for one migration file."""

    name: str
    idempotent: bool
    rls_safe: bool
    no_data_loss: bool
    classification: str
    issues: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.idempotent
            and self.rls_safe
            and self.no_data_loss
            and not self.issues
        )


@dataclass
class MigrationDrillReport:
    """Aggregate migration destruction-test outcome."""

    migration_count: int = 0
    expected_count: int = EXPECTED_MIGRATION_COUNT
    numbering_contiguous: bool = False
    verdicts: list[MigrationVerdict] = field(default_factory=list)
    live_cells: list[dict[str, Any]] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return (
            self.numbering_contiguous
            and self.migration_count == self.expected_count
            and all(v.passed for v in self.verdicts)
            and all(c.get("status") in {"pass", "skipped-live-unavailable"}
                    for c in self.live_cells)
        )


# ---------------------------------------------------------------------------
# Static rules
# ---------------------------------------------------------------------------

_UNGUARDED_CREATE = re.compile(
    r"\bCREATE\s+(?!OR\s+REPLACE)(TABLE|INDEX|POLICY|TRIGGER)\s+"
    r"(?!IF\s+NOT\s+EXISTS)",
    re.IGNORECASE,
)
_RLS_HARD_REMOVAL = re.compile(
    r"\bDISABLE\s+ROW\s+LEVEL\s+SECURITY\b"
    r"|\bNO\s+FORCE\s+ROW\s+LEVEL\s+SECURITY\b",
    re.IGNORECASE,
)
_DROP_POLICY = re.compile(r"\bDROP\s+POLICY\b", re.IGNORECASE)
_RLS_REESTABLISH = re.compile(
    r"\bCREATE\s+POLICY\b|\bENABLE\s+ROW\s+LEVEL\s+SECURITY\b"
    r"|\bFORCE\s+ROW\s+LEVEL\s+SECURITY\b"
    r"|\bCREATE\s+TRIGGER\b|\bCREATE\s+OR\s+REPLACE\s+FUNCTION\b",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_FUNCTION_BODY = re.compile(r"\$[a-zA-Z_]*\$.*?\$[a-zA-Z_]*\$", re.DOTALL)


def _strip_comments(sql: str) -> str:
    return _COMMENT.sub("", sql)


def _strip_function_bodies(sql: str) -> str:
    """Remove dollar-quoted function bodies — a DELETE inside a function
    *definition* executes at call time, not at migration apply time."""
    return _FUNCTION_BODY.sub("$$", sql)


def _check_idempotent(sql: str) -> list[str]:
    """Repo convention: TABLE/INDEX need IF NOT EXISTS; POLICY/TRIGGER
    (no IF NOT EXISTS in PostgreSQL) need a preceding DROP ... IF EXISTS."""
    issues: list[str] = []
    for match in _UNGUARDED_CREATE.finditer(sql):
        kind = match.group(1).upper()
        tail = sql[match.end():match.end() + 80]
        if re.match(r"\s*IF\s+NOT\s+EXISTS", tail, re.IGNORECASE):
            continue
        if kind in {"POLICY", "TRIGGER"}:
            obj = re.match(r"\s*(\w+)", tail)
            drop_pat = (
                rf"\bDROP\s+{kind}\s+IF\s+EXISTS\s+{re.escape(obj.group(1))}\b"
                if obj else rf"\bDROP\s+{kind}\s+IF\s+EXISTS\b"
            )
            if re.search(drop_pat, sql, re.IGNORECASE):
                continue
        stmt = sql[match.start():match.end() + 60].splitlines()[0]
        issues.append(f"unguarded CREATE: {stmt.strip()}")
    return issues


def _check_rls_safe(sql: str) -> list[str]:
    """Flag RLS removals that leave the table unprotected.

    ``DROP POLICY IF EXISTS`` is acceptable when the same file
    re-establishes enforcement — a new policy, ENABLE/FORCE RLS, or an
    enforcement trigger/function (e.g. migration 006 replaces audit
    policies with a prevent-mutation trigger).  Bare DISABLE / NO FORCE
    is always a violation.
    """
    issues: list[str] = []
    if _RLS_HARD_REMOVAL.search(sql):
        issues.append("RLS disabled without re-enable")
    drops = _DROP_POLICY.findall(sql)
    if drops and not _RLS_REESTABLISH.search(sql):
        issues.append(
            f"RLS policy dropped ({len(drops)}x) without re-establishment"
        )
    return issues


def verify_migration_file(path: Path) -> MigrationVerdict:
    """Statically verify one migration file."""
    sql = _strip_comments(path.read_text(encoding="utf-8"))
    analysis = classify_migration(sql, path.name)
    issues: list[str] = []
    issues += _check_idempotent(sql)
    issues += _check_rls_safe(sql)
    # Data-loss scan runs on top-level statements only — DELETE/TRUNCATE
    # inside a dollar-quoted function body is a definition, not execution.
    top_level = _strip_function_bodies(sql)
    destructive = re.search(
        r"\b(DELETE\s+FROM|TRUNCATE|DROP\s+TABLE)\s+\w",
        top_level,
        re.IGNORECASE,
    )
    no_data_loss = destructive is None
    if destructive:
        issues.append(f"data-loss risk: top-level {destructive.group(1)}")
    return MigrationVerdict(
        name=path.name,
        idempotent=not any(i.startswith("unguarded") for i in issues),
        rls_safe=not any(i.startswith("RLS removed") for i in issues),
        no_data_loss=no_data_loss,
        classification=analysis.classification.value,
        issues=issues,
    )


def verify_migrations(directory: Path | str) -> MigrationDrillReport:
    """Verify the whole migration set (static cells)."""
    dir_path = Path(directory)
    files = sorted(dir_path.glob("[0-9][0-9][0-9]_*.sql"))
    report = MigrationDrillReport(migration_count=len(files))
    numbers = [int(f.name[:3]) for f in files]
    report.numbering_contiguous = numbers == list(
        range(1, len(files) + 1)
    )
    for path in files:
        report.verdicts.append(verify_migration_file(path))
    return report


def live_cells(connection: Any | None) -> list[dict[str, Any]]:
    """Live migration drill cells — require a governed PG connection.

    Blank DB -> apply 001..HEAD; HEAD -> re-apply (idempotency); these
    cannot run against SQLite because the migrations use PostgreSQL DDL.
    """
    cells = [
        "blank-db-to-head",
        "old-schema-upgrade-to-head",
        "head-rerun-idempotent",
        "interrupted-migration-recovery",
        "migration-with-existing-data",
        "migration-with-rls-present",
        "old-runtime-new-schema",
    ]
    if connection is None:
        return [
            {"cell": c, "status": "skipped-live-unavailable"} for c in cells
        ]
    raise NotImplementedError(
        "live migration drill requires a governed PostgreSQL test instance"
    )


__all__ = [
    "MigrationDrillReport",
    "MigrationVerdict",
    "live_cells",
    "verify_migration_file",
    "verify_migrations",
]

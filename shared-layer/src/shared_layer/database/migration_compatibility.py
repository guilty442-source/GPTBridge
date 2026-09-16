"""Migration Compatibility Checker (A44/E30).

Classifies each migration as backward-compatible or breaking before it is
applied.  Breaking migrations require an explicit upgrade path and cannot
silently destroy old runtime.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class MigrationClassification(str, Enum):
    BACKWARD_COMPATIBLE = "backward-compatible"
    BREAKING = "breaking"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MigrationAnalysis:
    """Analysis of a single migration file."""

    name: str
    classification: MigrationClassification
    breaking_changes: tuple[str, ...]
    upgrade_notes: str = ""


# Patterns that indicate a breaking change
_BREAKING_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bDROP\s+TABLE\b", "drops a table"),
    (r"\bDROP\s+COLUMN\b", "drops a column"),
    (r"\bALTER\s+COLUMN\b.*\bTYPE\b", "changes column type"),
    (r"\bALTER\s+COLUMN\b.*\bSET\s+NOT\s+NULL\b", "adds NOT NULL constraint"),
    (r"\bALTER\s+COLUMN\b.*\bDROP\s+NOT\s+NULL\b", "drops NOT NULL constraint"),
    (r"\bRENAME\s+TO\b", "renames an object"),
    (r"\bRENAME\s+COLUMN\b", "renames a column"),
    (r"\bDROP\s+INDEX\b", "drops an index"),
    (r"\bDROP\s+POLICY\b", "drops an RLS policy"),
    (r"\bDROP\s+FUNCTION\b", "drops a function"),
    (r"\bDROP\s+TRIGGER\b", "drops a trigger"),
    (r"\bREVOKE\b", "revokes privileges"),
    (r"\bDELETE\s+FROM\b", "deletes data"),
    (r"\bTRUNCATE\b", "truncates a table"),
)

# Patterns that indicate backward-compatible additions
_COMPATIBLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bADD\s+COLUMN\b.*\bNOT\s+NULL\b.*\bDEFAULT\b", "adds column with default"),
    (r"\bADD\s+COLUMN\b.*\bIF\s+NOT\s+EXISTS\b", "adds column if not exists"),
    (r"\bCREATE\s+TABLE\b.*\bIF\s+NOT\s+EXISTS\b", "creates table if not exists"),
    (r"\bCREATE\s+INDEX\b.*\bIF\s+NOT\s+EXISTS\b", "creates index if not exists"),
    (r"\bCREATE\s+OR\s+REPLACE\s+FUNCTION\b", "creates or replaces function"),
    (r"\bCREATE\s+OR\s+REPLACE\s+VIEW\b", "creates or replaces view"),
    (r"\bGRANT\b", "grants privileges"),
    (r"\bCREATE\s+POLICY\b.*\bIF\s+NOT\s+EXISTS\b", "creates policy if not exists"),
    (r"\bINSERT\s+INTO\b.*\bON\s+CONFLICT\b", "idempotent insert"),
)


def classify_migration(sql_text: str, name: str = "") -> MigrationAnalysis:
    """Classify a migration SQL file as compatible or breaking."""
    breaking: list[str] = []
    compatible: list[str] = []

    upper = sql_text.upper()
    for pattern, description in _BREAKING_PATTERNS:
        if re.search(pattern, upper):
            breaking.append(description)

    for pattern, description in _COMPATIBLE_PATTERNS:
        if re.search(pattern, upper):
            compatible.append(description)

    if breaking:
        classification = MigrationClassification.BREAKING
    elif compatible:
        classification = MigrationClassification.BACKWARD_COMPATIBLE
    else:
        classification = MigrationClassification.UNKNOWN

    upgrade_notes = ""
    if classification == MigrationClassification.BREAKING:
        upgrade_notes = (
            "Breaking migration requires explicit upgrade path. "
            "Ensure: (1) old runtime can still start, (2) data migration "
            "script exists, (3) rollback procedure is documented."
        )

    return MigrationAnalysis(
        name=name,
        classification=classification,
        breaking_changes=tuple(breaking),
        upgrade_notes=upgrade_notes,
    )


def analyze_migration_directory(directory: Path | str) -> list[MigrationAnalysis]:
    """Analyze all migration files in a directory."""
    dir_path = Path(directory)
    results: list[MigrationAnalysis] = []
    for sql_file in sorted(dir_path.glob("*.sql")):
        text = sql_file.read_text(encoding="utf-8")
        results.append(classify_migration(text, sql_file.name))
    return results


__all__ = [
    "MigrationClassification",
    "MigrationAnalysis",
    "classify_migration",
    "analyze_migration_directory",
]

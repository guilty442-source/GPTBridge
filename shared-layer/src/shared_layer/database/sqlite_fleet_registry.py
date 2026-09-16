"""SQLite Fleet Registry (A44/E30 + A8/E21).

Central registry of all SQLite databases in the fleet.  Records path, owner,
schema_version, size, wal_size, integrity, last_reconcile, last_backup for
each database.  This prevents the fleet from becoming an unmanaged sprawl
of private databases.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class SqliteFleetEntry:
    """One SQLite database in the fleet."""

    path: str
    owner: str  # module_id or tool_id
    schema_version: int = 0
    size_bytes: int = 0
    wal_size_bytes: int = 0
    integrity_ok: bool = False
    last_reconcile_at: str = ""
    last_backup_at: str = ""
    last_inspected_at: str = ""
    error: str = ""


@dataclass
class FleetInspectionResult:
    """Result of inspecting the entire SQLite fleet."""

    entries: list[SqliteFleetEntry] = field(default_factory=list)
    total_databases: int = 0
    total_size_bytes: int = 0
    integrity_failures: int = 0
    stale_databases: int = 0  # not reconciled in 24h

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_databases": self.total_databases,
            "total_size_bytes": self.total_size_bytes,
            "total_size_mb": round(self.total_size_bytes / (1024 * 1024), 2),
            "integrity_failures": self.integrity_failures,
            "stale_databases": self.stale_databases,
            "entries": [
                {
                    "path": e.path,
                    "owner": e.owner,
                    "schema_version": e.schema_version,
                    "size_bytes": e.size_bytes,
                    "wal_size_bytes": e.wal_size_bytes,
                    "integrity_ok": e.integrity_ok,
                    "last_reconcile_at": e.last_reconcile_at,
                    "last_backup_at": e.last_backup_at,
                    "last_inspected_at": e.last_inspected_at,
                    "error": e.error,
                }
                for e in self.entries
            ],
        }


def _inspect_one(db_path: Path) -> SqliteFleetEntry:
    """Inspect a single SQLite database file."""
    entry = SqliteFleetEntry(
        path=str(db_path),
        owner=db_path.parent.name,
        last_inspected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    try:
        entry.size_bytes = os.path.getsize(db_path)
    except OSError:
        entry.error = "file_not_found"
        return entry

    # Check for WAL file
    wal_path = str(db_path) + "-wal"
    try:
        entry.wal_size_bytes = os.path.getsize(wal_path)
    except OSError:
        entry.wal_size_bytes = 0

    # Open and inspect
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            # Integrity check
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            entry.integrity_ok = integrity and integrity[0] == "ok"

            # Schema version
            try:
                row = conn.execute(
                    "SELECT schema_version FROM schema_version ORDER BY schema_version DESC LIMIT 1"
                ).fetchone()
                if row:
                    entry.schema_version = int(row[0])
            except sqlite3.OperationalError:
                entry.schema_version = 0  # no schema_version table

            # Last reconcile
            try:
                row = conn.execute(
                    "SELECT MAX(reconciled_at) FROM reconcile_state WHERE reconcile_status = 'in-sync'"
                ).fetchone()
                if row and row[0]:
                    entry.last_reconcile_at = str(row[0])
            except sqlite3.OperationalError:
                pass  # no reconcile_state table

            # Owner from module_metadata
            try:
                row = conn.execute(
                    "SELECT module_id FROM module_metadata LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    entry.owner = str(row[0])
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        entry.error = f"database_error: {exc}"
        entry.integrity_ok = False
    except Exception as exc:
        entry.error = f"inspect_error: {exc}"
        entry.integrity_ok = False

    return entry


def discover_sqlite_databases(search_roots: list[Path | str]) -> list[Path]:
    """Discover all .sqlite/.db files under the given search roots."""
    found: list[Path] = []
    seen: set[str] = set()
    for root in search_roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for pattern in ("*.sqlite", "*.sqlite3", "*.db"):
            for path in root_path.rglob(pattern):
                resolved = str(path.resolve())
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(path)
    return found


def inspect_fleet(search_roots: list[Path | str]) -> FleetInspectionResult:
    """Inspect the entire SQLite fleet under the given search roots.

    This is read-only — it never mutates any database.
    """
    databases = discover_sqlite_databases(search_roots)
    result = FleetInspectionResult()
    now = datetime.now(timezone.utc)

    for db_path in databases:
        entry = _inspect_one(db_path)
        result.entries.append(entry)
        result.total_databases += 1
        result.total_size_bytes += entry.size_bytes
        if not entry.integrity_ok:
            result.integrity_failures += 1
        # Stale = not reconciled in 24h
        if entry.last_reconcile_at:
            try:
                reconciled = datetime.fromisoformat(entry.last_reconcile_at)
                if (now - reconciled).total_seconds() > 86400:
                    result.stale_databases += 1
            except (ValueError, TypeError):
                result.stale_databases += 1
        else:
            result.stale_databases += 1

    return result


__all__ = [
    "SqliteFleetEntry",
    "FleetInspectionResult",
    "discover_sqlite_databases",
    "inspect_fleet",
]

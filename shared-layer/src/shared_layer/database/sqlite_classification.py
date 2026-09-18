"""SQLite Classification (migration 037 + E8).

Classify each SQLite database into Class A/B/C/D with different
synchronous, backup, integrity check, retention, and reconcile policies.

Class A: governance codex / high-integrity
Class B: module-private formal state
Class C: runtime / checkpoint
Class D: cache / fallback

Usage:
    from shared_layer.database.sqlite_classification import register, get_class

    with connection_manager.connection() as conn:
        register(conn, module_id="governance", database_path="codex.sqlite3",
                 db_class="A", description="governance codex")
        info = get_class(conn, module_id="governance",
                        database_path="codex.sqlite3")

Codex basis:
    A8/E21  — SQLite: owner-private-operational-state.
    A44/E30 — four-functions-local.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg import Connection

_REGISTER = "SELECT gptbridge_index.upsert_sqlite_class(%s, %s, %s, %s)"
_GET_CLASS = (
    "SELECT module_id, database_path, db_class, synchronous_setting, "
    "backup_frequency_seconds, integrity_check_frequency_seconds, "
    "retention_days, reconcile_required, description "
    "FROM gptbridge_index.sqlite_database_class "
    "WHERE module_id = %s AND database_path = %s"
)
_LIST_BY_CLASS = (
    "SELECT module_id, database_path, description "
    "FROM gptbridge_index.sqlite_database_class WHERE db_class = %s "
    "ORDER BY module_id, database_path"
)


def register(
    connection: Connection[Any],
    *,
    module_id: str,
    database_path: str,
    db_class: str,
    description: Optional[str] = None,
) -> None:
    """Register or update a SQLite database's classification."""
    connection.execute(_REGISTER, (module_id, database_path, db_class, description))


def _field(row: Any, index: int, key: str) -> Any:
    """Row access that tolerates both tuple and dict_row connections."""
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


def get_class(
    connection: Connection[Any],
    *,
    module_id: str,
    database_path: str,
) -> Optional[dict[str, Any]]:
    """Get the classification info for a SQLite database."""
    row = connection.execute(
        _GET_CLASS, (module_id, database_path)
    ).fetchone()
    if not row:
        return None
    return {
        "module_id": str(_field(row, 0, "module_id")),
        "database_path": str(_field(row, 1, "database_path")),
        "db_class": str(_field(row, 2, "db_class")),
        "synchronous": str(_field(row, 3, "synchronous_setting")),
        "backup_frequency_seconds": int(_field(row, 4, "backup_frequency_seconds")),
        "integrity_check_frequency_seconds": int(
            _field(row, 5, "integrity_check_frequency_seconds")
        ),
        "retention_days": int(_field(row, 6, "retention_days")),
        "reconcile_required": bool(_field(row, 7, "reconcile_required")),
        "description": str(_field(row, 8, "description") or ""),
    }


def list_by_class(
    connection: Connection[Any],
    *,
    db_class: str,
) -> list[dict[str, str]]:
    """List all SQLite databases of a given class."""
    rows = connection.execute(_LIST_BY_CLASS, (db_class,)).fetchall()
    return [
        {
            "module_id": str(_field(r, 0, "module_id")),
            "database_path": str(_field(r, 1, "database_path")),
            "description": str(_field(r, 2, "description") or ""),
        }
        for r in rows
    ]


__all__ = ["register", "get_class", "list_by_class"]

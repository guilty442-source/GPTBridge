"""Generation Fence Helper (migration 016/025 + C2).

Manages the backend generation counter: bumps it after restore, migration,
or role rotation, and checks whether a connection's declared generation
is still valid for writing.

The generation fence (migration 016) ensures that old connections holding
a stale generation are rejected from writing, even if the connection is
still alive.  This module is the runtime entry point for bumping and
checking generation.

Usage:
    from shared_layer.database.generation_fence import (
        get_current_generation,
        bump_generation,
        is_connection_stale,
    )

    with connection_manager.connection() as conn:
        gen = get_current_generation(conn)
        set_provenance(conn, generation=gen)
        # ... write ...

    # After restore/migration:
    bump_generation(conn, reason="post-restore", set_by="rebuild_certifier")

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A44/E30 — four-functions-local; UNAVAILABLE:declare-closed-not-replace.
    A46/E22 — Audit: mandatory-ledger.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg import Connection

_GET_CURRENT = "SELECT gptbridge_index.current_backend_generation()"
_BUMP = "SELECT gptbridge_index.bump_backend_generation(%s, %s)"
_GET_SQLITE_STALE = (
    "SELECT module_id, database_path, backend_generation "
    "FROM gptbridge_index.sqlite_generation WHERE stale = true "
    "ORDER BY updated_at"
)
_UPSERT_SQLITE = "SELECT gptbridge_index.upsert_sqlite_generation(%s, %s, %s)"


def get_current_generation(connection: Connection[Any]) -> int:
    """Get the current backend generation from PostgreSQL."""
    row = connection.execute(_GET_CURRENT).fetchone()
    return int(row[0]) if row and row[0] else 1


def bump_generation(
    connection: Connection[Any],
    *,
    reason: str,
    set_by: str,
) -> int:
    """Bump the backend generation after restore/migration/role-rotation.

    Returns the new generation number.  All connections holding the old
    generation will be rejected from writing until they refresh.
    """
    row = connection.execute(_BUMP, (reason, set_by)).fetchone()
    return int(row[0]) if row and row[0] else 1


def is_connection_stale(
    connection: Connection[Any],
    declared_generation: int,
) -> bool:
    """Check if a connection's declared generation is stale."""
    current = get_current_generation(connection)
    return declared_generation < current


def get_stale_sqlite_databases(connection: Connection[Any]) -> list[dict[str, Any]]:
    """List SQLite databases whose generation is behind the current one."""
    rows = connection.execute(_GET_SQLITE_STALE).fetchall()
    return [
        {
            "module_id": str(r[0]),
            "database_path": str(r[1]),
            "backend_generation": int(r[2]),
        }
        for r in rows
    ]


def upsert_sqlite_generation(
    connection: Connection[Any],
    *,
    module_id: str,
    database_path: str,
    generation: int,
) -> None:
    """Record a SQLite database's current generation in PostgreSQL."""
    connection.execute(_UPSERT_SQLITE, (module_id, database_path, generation))


__all__ = [
    "get_current_generation",
    "bump_generation",
    "is_connection_stale",
    "get_stale_sqlite_databases",
    "upsert_sqlite_generation",
]

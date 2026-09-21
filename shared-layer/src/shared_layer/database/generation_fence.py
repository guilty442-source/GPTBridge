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
_SET_GENERATION = "SELECT set_config('gptbridge.connection_generation', %s, false)"
_GET_SQLITE_STALE = (
    "SELECT module_id, database_path, backend_generation "
    "FROM gptbridge_index.sqlite_generation WHERE stale = true "
    "ORDER BY updated_at"
)
_UPSERT_SQLITE = "SELECT gptbridge_index.upsert_sqlite_generation(%s, %s, %s)"

# Qdrant generation sync (Spec 8: Qdrant generation sync)
_GET_QDRANT_STALE = (
    "SELECT collection_name, backend_generation "
    "FROM gptbridge_index.qdrant_generation WHERE stale = true "
    "ORDER BY updated_at"
)
_UPSERT_QDRANT = "SELECT gptbridge_index.upsert_qdrant_generation(%s, %s)"


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


def set_provenance(connection: Connection[Any], *, generation: int | None = None) -> int:
    """Declare the connection's backend generation (A8/E21, migration 016).

    The declaration is session-scoped (``set_config(..., false)``) so pooled
    connections keep a declared generation across transactions; once the
    backend generation is bumped (restore/migration/rotation) a stale declared
    generation makes the fence reject every subsequent write on that
    connection until it refreshes.

    Called before transport/resource writes; when ``generation`` is omitted the
    current backend generation is read first.
    """
    value = get_current_generation(connection) if generation is None else int(generation)
    connection.execute(_SET_GENERATION, (str(value),))
    return value


def declare_connection_generation(connection: Connection[Any]) -> int:
    """Read the current backend generation and declare it on the connection."""
    return set_provenance(connection)


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


def get_stale_qdrant_collections(connection: Connection[Any]) -> list[dict[str, Any]]:
    """List Qdrant collections whose generation is behind the current one."""
    rows = connection.execute(_GET_QDRANT_STALE).fetchall()
    return [
        {
            "collection_name": str(r[0]),
            "backend_generation": int(r[1]),
        }
        for r in rows
    ]


def upsert_qdrant_generation(
    connection: Connection[Any],
    *,
    collection_name: str,
    generation: int,
) -> None:
    """Record a Qdrant collection's current generation in PostgreSQL."""
    connection.execute(_UPSERT_QDRANT, (collection_name, generation))


def sync_qdrant_generation(
    connection: Connection[Any],
    *,
    qdrant_client: Any,
) -> list[dict[str, Any]]:
    """Sync Qdrant collection generations with PostgreSQL.

    Iterates collections via qdrant_client, reads their generation
    (from collection metadata or point payload), and upserts into PG.
    Returns list of stale collections.
    """
    # Get current backend generation
    current_gen = get_current_generation(connection)

    # Get collections from Qdrant (requires client with get_collections method)
    get_collections = getattr(qdrant_client, "get_collections", None)
    if not callable(get_collections):
        return []

    collections = get_collections()
    stale = []
    for coll in collections:
        coll_name = getattr(coll, "name", None) or coll.get("name") if isinstance(coll, dict) else None
        if not coll_name:
            continue

        # Try to get generation from collection metadata
        coll_gen = 1
        get_info = getattr(qdrant_client, "get_collection", None)
        if callable(get_info):
            try:
                info = get_info(coll_name)
                # Check config or custom payload for generation
                config = getattr(info, "config", None) or info.get("config") if isinstance(info, dict) else None
                if config:
                    params = getattr(config, "params", None) or config.get("params") if isinstance(config, dict) else None
                    if params:
                        coll_gen = int(params.get("generation", 1))
            except Exception:
                pass

        # Upsert to PG
        upsert_qdrant_generation(connection, collection_name=coll_name, generation=coll_gen)

        # Check if stale
        if coll_gen < current_gen:
            stale.append({"collection_name": coll_name, "backend_generation": coll_gen})

    return stale


__all__ = [
    "bump_generation",
    "declare_connection_generation",
    "get_current_generation",
    "get_stale_sqlite_databases",
    "get_stale_qdrant_collections",
    "is_connection_stale",
    "set_provenance",
    "sync_qdrant_generation",
    "upsert_qdrant_generation",
    "upsert_sqlite_generation",
]

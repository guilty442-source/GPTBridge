"""Stage-schema naming and lifecycle for the PostgreSQL codex (A185 split).

Extracted from ``codex_postgresql_stage`` (source-size contract): owns
stage-schema identity (``_stage_name`` / ``unique_stage_name``), the
crash-orphan sweep, the admin connection and the bounded catalog reads
the artifact codec and the connection facade share.

Everything here is fail-closed: no DSN, no schema, no mutation of the live
authority (writes only ever land inside ``*_stage_*`` schemas).
"""

from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any, Final

import psycopg
from psycopg import sql as pg_sql

try:
    from .codex_postgresql_dsn import _IDENTIFIER, CODEX_SCHEMA, admin_dsn
except ImportError:  # flat script import (execution/ on sys.path)
    from codex_postgresql_dsn import _IDENTIFIER, CODEX_SCHEMA, admin_dsn

STAGE_PREFIX = "codex_stage_"


def _stage_name(token: object) -> str:
    # PostgreSQL identifiers cap at 63 bytes; the ``{schema}_{STAGE_PREFIX}``
    # prefix is 28 chars, so tokens beyond 35 chars would be silently
    # truncated by the server — which would also cut any uniqueness suffix.
    text = re.sub(r"[^A-Za-z0-9_]", "_", str(token))[:35].strip("_") or "stage"
    return f"{CODEX_SCHEMA}_{STAGE_PREFIX}{text}"


STALE_STAGE_TTL_SECONDS: Final[float] = 24 * 3600.0

# ``unique_stage_name`` layout after ``_stage_name`` sanitisation:
# ``{tag}_{epoch}_{pid_hex}_{rand4}`` — the epoch field is the sweep key.
_UNIQUE_STAGE_NAME = re.compile(
    r"^" + re.escape(f"{CODEX_SCHEMA}_{STAGE_PREFIX}")
    + r"[a-z0-9]+_(\d{9,})_[a-z0-9]+_[a-z0-9]{4}$"
)


def unique_stage_name(tag: object = "x") -> str:
    """Collision-free stage schema name for one throwaway materialization.

    A deterministic per-artifact name makes every concurrent opener of the
    same artifact share one schema; each ``DROP SCHEMA``/materialize then
    destroys a sibling's in-flight copy (the audit's parallel codex checks
    hit exactly this).  ``tag`` keeps the name greppable; epoch + pid +
    random isolate every open and let :func:`sweep_stale_stage_schemas`
    reap crash leftovers (bounded growth, A178).
    """
    clean = re.sub(r"[^a-z0-9]", "", str(tag).lower())[:8] or "x"
    return _stage_name(
        f"{clean}_{int(time.time())}_{os.getpid():x}_{uuid.uuid4().hex[:4]}"
    )


def sweep_stale_stage_schemas(
    max_age_seconds: float = STALE_STAGE_TTL_SECONDS,
) -> list[str]:
    """Drop crash-orphaned unique stage schemas older than ``max_age_seconds``.

    ``unique_stage_name`` removes the name collision — and with it the
    incidental self-heal where a later run re-dropped a leftover schema.
    This sweep restores the bound: only schemas carrying the unique-name
    layout are eligible, so caller-named stage schemas are never touched
    and the TTL keeps an in-flight open safe.
    """
    cutoff = time.time() - max_age_seconds
    dropped: list[str] = []
    with _admin() as connection:
        rows = connection.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE %s",
            (f"{CODEX_SCHEMA}_{STAGE_PREFIX}%",),
        ).fetchall()
        for (name,) in rows:
            match = _UNIQUE_STAGE_NAME.match(str(name))
            if match is None or int(match.group(1)) > cutoff:
                continue
            connection.execute(  # sql-ok: governed stale-stage cleanup — catalog-bounded set
                pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    pg_sql.Identifier(str(name))
                )
            )
            dropped.append(str(name))
    return dropped


def _admin() -> psycopg.Connection[Any]:
    connection = psycopg.connect(admin_dsn(), autocommit=True)
    return connection


def schema_exists(schema: str) -> bool:
    with _admin() as connection:
        row = connection.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name=%s",
            (schema,),
        ).fetchone()
    return row is not None


def create_empty_schema(schema: str) -> None:
    if not _IDENTIFIER.fullmatch(schema):
        raise RuntimeError(f"STAGE_SCHEMA_NAME_INVALID:{schema}")
    with _admin() as connection:
        connection.execute(
            pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                pg_sql.Identifier(schema)
            )
        )
        connection.execute(
            pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema))
        )


def drop_schema(schema: str) -> None:
    if not _IDENTIFIER.fullmatch(schema) or STAGE_PREFIX not in schema:
        raise RuntimeError(f"STAGE_SCHEMA_NAME_INVALID:{schema}")
    with _admin() as connection:
        connection.execute(
            pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                pg_sql.Identifier(schema)
            )
        )


def _schema_tables(connection: Any, schema: str) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE' "
            "ORDER BY table_name",
            (schema,),
        )
    ]


def clone_schema(source_schema: str, target_schema: str) -> list[str]:
    """Clone every base table (structure + rows) into a fresh stage schema."""
    for name in (source_schema, target_schema):
        if not _IDENTIFIER.fullmatch(name):
            raise RuntimeError(f"STAGE_SCHEMA_NAME_INVALID:{name}")
    with _admin() as connection:
        tables = _schema_tables(connection, source_schema)
        if not tables:
            raise RuntimeError(f"STAGE_SOURCE_EMPTY:{source_schema}")
        connection.execute(
            pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                pg_sql.Identifier(target_schema)
            )
        )
        connection.execute(
            pg_sql.SQL("CREATE SCHEMA {}").format(
                pg_sql.Identifier(target_schema)
            )
        )
        for table in tables:
            connection.execute(  # sql-ok: governed schema clone — bounded catalog set, LIKE mirrors source schema
                pg_sql.SQL("CREATE TABLE {}.{} (LIKE {}.{} INCLUDING ALL)").format(
                    pg_sql.Identifier(target_schema),
                    pg_sql.Identifier(table),
                    pg_sql.Identifier(source_schema),
                    pg_sql.Identifier(table),
                )
            )
            connection.execute(  # sql-ok: schema-clone copy — SELECT * columns are identical to LIKE-cloned target by construction
                pg_sql.SQL("INSERT INTO {}.{} SELECT * FROM {}.{}").format(
                    pg_sql.Identifier(target_schema),
                    pg_sql.Identifier(table),
                    pg_sql.Identifier(source_schema),
                    pg_sql.Identifier(table),
                )
            )
    return tables


def clone_authority(target_schema: str) -> list[str]:
    return clone_schema(CODEX_SCHEMA, target_schema)


def stage_table_names(schema: str) -> frozenset[str]:
    with _admin() as connection:
        return frozenset(_schema_tables(connection, schema))

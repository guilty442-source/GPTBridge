"""One-way SQLite predecessor import into the PostgreSQL Codex authority.

Extracted from ``codex_postgresql`` (source-size contract): owns the
atomic schema-replace transaction — advisory-lock serialization,
monotonic-version regression guard, per-table DDL/row transfer, and the
authority-state bookkeeping row.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

try:
    from .codex_postgresql_dsn import (
        _IDENTIFIER,
        CODEX_AUTHORITY_URI,
        CODEX_SCHEMA,
        admin_dsn,
    )
except ImportError:  # flat script import (execution/ on sys.path)
    from codex_postgresql_dsn import (
        _IDENTIFIER,
        CODEX_AUTHORITY_URI,
        CODEX_SCHEMA,
        admin_dsn,
    )


def _pg_type(declared_type: str, values: list[Any]) -> str:
    value = declared_type.upper()
    populated = [item for item in values if item is not None]
    if "INT" in value and all(isinstance(item, int) and not isinstance(item, bool) for item in populated):
        return "BIGINT"
    if any(token in value for token in ("REAL", "FLOA", "DOUB")) and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in populated
    ):
        return "DOUBLE PRECISION"
    if "BLOB" in value:
        return "BYTEA"
    return "TEXT"


def _source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _version_units(value: str) -> int | None:
    """Orderable units for legacy ``x.yyyyy`` or ISO-8601 UTC versions."""
    text = str(value or "").strip()
    whole, sep, fraction = text.partition(".")
    if sep and whole.isdigit() and fraction.isdigit() and len(fraction) == 5:
        return int(whole) * 100_000 + int(fraction)
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return None


def _version_regresses(source_version: str, current_version: str) -> bool:
    """True when the source is provably older than the live authority."""
    source_units = _version_units(source_version)
    current_units = _version_units(current_version)
    if source_units is None or current_units is None:
        return True
    return source_units < current_units


def _read_predecessor(sqlite_connection: sqlite3.Connection) -> tuple[list[str], dict[str, Any]]:
    """Return (table names, metadata row map) from the sealed predecessor."""
    tables = [
        str(row[0])
        for row in sqlite_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    if not tables or "metadata" not in tables:
        raise RuntimeError("CODEX_SQLITE_PREDECESSOR_INVALID")
    metadata = dict(sqlite_connection.execute("SELECT key, value FROM metadata"))
    return tables, metadata


def _guard_import(target: psycopg.Connection[Any], source_version: str) -> None:
    """Serialize concurrent imports and refuse version regressions.

    Two governed executors racing the shared authority must not interleave
    DROP/CREATE (duplicate-table race observed 2026-09-25 when a service
    tick and a CLI run executed the same request).  The xact-scoped lock
    releases automatically on commit/abort.  The monotonic-version guard
    is fail-closed: the authority never regresses.
    """
    target.execute(
        "SELECT pg_advisory_xact_lock(hashtext('gptbridge.codex.import'))"
    )
    try:
        row = target.execute(
            sql.SQL("SELECT codex_version FROM {}.codex_authority_state").format(
                sql.Identifier(CODEX_SCHEMA)
            )
        ).fetchone()
    except psycopg.errors.Error:
        # Authority state unreadable (fresh init, missing schema/table):
        # nothing to regress against.
        row = None
        target.rollback()
    if row is not None:
        current_version = str(row[0])
        if _version_regresses(source_version, current_version):
            raise RuntimeError(
                "CODEX_VERSION_REGRESSION:"
                f"{source_version}<{current_version}"
            )


def _table_definitions(
    columns: list[Any], rows: list[Any]
) -> tuple[list[sql.Composable], list[str], list[str]]:
    """Build column definitions, inferred PG types and column names."""
    definitions: list[sql.Composable] = []
    pg_types: list[str] = []
    primary = sorted(
        ((int(row[5]), str(row[1])) for row in columns if int(row[5])),
        key=lambda item: item[0],
    )
    for column_index, (_, name, declared_type, not_null, _default, _pk) in enumerate(columns):
        values = [row[column_index] for row in rows]
        pg_type = _pg_type(str(declared_type), values)
        pg_types.append(pg_type)
        definition = sql.SQL("{} {}").format(
            sql.Identifier(str(name)), sql.SQL(pg_type)
        )
        if int(not_null):
            definition += sql.SQL(" NOT NULL")
        definitions.append(definition)
    if primary:
        definitions.append(
            sql.SQL("PRIMARY KEY ({})").format(
                sql.SQL(", ").join(sql.Identifier(name) for _, name in primary)
            )
        )
    names = [str(row[1]) for row in columns]
    return definitions, pg_types, names


def _insert_table_rows(
    target: psycopg.Connection[Any],
    table: str,
    names: list[str],
    rows: list[Any],
    pg_types: list[str],
) -> None:
    if not rows:
        return
    converted_rows = [
        tuple(
            None if value is None else str(value) if pg_types[index] == "TEXT" else value
            for index, value in enumerate(row)
        )
        for row in rows
    ]
    statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(CODEX_SCHEMA),
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(name) for name in names),
        sql.SQL(", ").join(sql.Placeholder() for _ in names),
    )
    with target.cursor() as cursor:
        cursor.executemany(statement, converted_rows)


def _import_table(
    target: psycopg.Connection[Any],
    sqlite_connection: sqlite3.Connection,
    table: str,
) -> int:
    """Create one table in the authority schema and copy its rows."""
    if not _IDENTIFIER.fullmatch(table):
        raise RuntimeError(f"CODEX_TABLE_IDENTIFIER_INVALID:{table}")
    columns = list(sqlite_connection.execute(f'PRAGMA table_info("{table}")'))  # sql-ok: table name validated by _IDENTIFIER
    if not columns:
        raise RuntimeError(f"CODEX_TABLE_SCHEMA_MISSING:{table}")
    rows = list(sqlite_connection.execute(f'SELECT * FROM "{table}"'))  # sql-ok: schema-agnostic full-table import copy, identifier validated by _IDENTIFIER
    definitions, pg_types, names = _table_definitions(columns, rows)
    target.execute(
        sql.SQL("CREATE TABLE {}.{} ({})").format(
            sql.Identifier(CODEX_SCHEMA), sql.Identifier(table), sql.SQL(", ").join(definitions)
        )
    )
    _insert_table_rows(target, table, names, rows, pg_types)
    return len(rows)


def _seal_authority(
    target: psycopg.Connection[Any],
    metadata: dict[str, Any],
    source_digest: str,
    table_count: int,
    total_rows: int,
) -> None:
    """Write the authority-state row and grant the runtime role."""
    target.execute(
        sql.SQL(
            "CREATE TABLE {}.codex_authority_state ("
            "authority_uri TEXT PRIMARY KEY, codex_version TEXT NOT NULL, "
            "source_sha256 TEXT NOT NULL, table_count BIGINT NOT NULL, "
            "row_count BIGINT NOT NULL, imported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ).format(sql.Identifier(CODEX_SCHEMA))
    )
    target.execute(
        sql.SQL("INSERT INTO {}.codex_authority_state "
                "(authority_uri,codex_version,source_sha256,table_count,row_count) VALUES (%s,%s,%s,%s,%s)").format(
            sql.Identifier(CODEX_SCHEMA)
        ),
        (CODEX_AUTHORITY_URI, str(metadata.get("codex_version", "")), source_digest, table_count, total_rows),
    )
    target.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO gptbridge_runtime").format(sql.Identifier(CODEX_SCHEMA)))
    target.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO gptbridge_runtime").format(sql.Identifier(CODEX_SCHEMA)))
    target.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO gptbridge_runtime").format(sql.Identifier(CODEX_SCHEMA)))


def import_sqlite_predecessor(source: Path) -> dict[str, Any]:
    """Atomically replace the PostgreSQL Codex schema from the sealed predecessor."""
    source = Path(source).resolve()
    sqlite_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        tables, metadata = _read_predecessor(sqlite_connection)
        source_digest = _source_hash(source)
        source_version = str(metadata.get("codex_version", ""))
        total_rows = 0
        with psycopg.connect(admin_dsn()) as target:
            _guard_import(target, source_version)
            target.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(CODEX_SCHEMA)))
            target.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(CODEX_SCHEMA)))
            target.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(CODEX_SCHEMA)))
            for table in tables:
                total_rows += _import_table(target, sqlite_connection, table)
            _seal_authority(target, metadata, source_digest, len(tables), total_rows)
        return {
            "codex_version": str(metadata.get("codex_version", "")),
            "source_sha256": source_digest,
            "table_count": len(tables),
            "row_count": total_rows,
        }
    finally:
        sqlite_connection.close()

"""Export/verify paths between the PostgreSQL authority and SQLite scratch.

Extracted from ``codex_postgresql`` (source-size contract): owns the
non-authoritative staged-export and the cell-level parity verifier.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from psycopg import sql

try:
    from .codex_postgresql_dsn import _IDENTIFIER, CODEX_SCHEMA
    from .codex_postgresql_pool import readonly_connection
except ImportError:  # flat script import (execution/ on sys.path)
    from codex_postgresql_dsn import _IDENTIFIER, CODEX_SCHEMA
    from codex_postgresql_pool import readonly_connection


def _sqlite_decl(pg_type: str) -> str:
    """Map a PostgreSQL column type back to a SQLite storage class."""
    value = pg_type.upper()
    if "INT" in value:
        return "INTEGER"
    if any(token in value for token in ("DOUBLE", "REAL", "FLOAT")):
        return "REAL"
    if "BYTEA" in value or "BLOB" in value:
        return "BLOB"
    return "TEXT"


def _authority_tables(source: Any) -> list[str]:
    return [
        str(row[0])
        for row in source.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE' "
            "AND table_name<>'codex_authority_state' ORDER BY table_name",
            (CODEX_SCHEMA,),
        )
    ]


def _export_table(source: Any, connection: sqlite3.Connection, table: str) -> None:
    """Copy one authority table (schema + rows) into the scratch file."""
    if not _IDENTIFIER.fullmatch(table):
        raise RuntimeError(f"CODEX_TABLE_IDENTIFIER_INVALID:{table}")
    columns = list(
        source.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema=%s "
            "AND table_name=%s ORDER BY ordinal_position",
            (CODEX_SCHEMA, table),
        )
    )
    names = [str(column[0]) for column in columns]
    definitions = []
    for name, pg_type, nullable in columns:
        definition = f'"{name}" {_sqlite_decl(str(pg_type))}'
        if str(nullable) == "NO":
            definition += " NOT NULL"
        definitions.append(definition)
    connection.execute(  # sql-ok: identifier from information_schema columns of a _IDENTIFIER-validated table name
        f'CREATE TABLE "{table}" ({", ".join(definitions)})'
    )
    rows = list(
        source.execute(
            sql.SQL("SELECT {} FROM {}.{}").format(
                sql.SQL(", ").join(
                    sql.Identifier(name) for name in names
                ),
                sql.Identifier(CODEX_SCHEMA),
                sql.Identifier(table),
            )
        )
    )
    if rows:
        converted = [
            tuple(
                bytes(value) if isinstance(value, memoryview) else value
                for value in row
            )
            for row in rows
        ]
        connection.executemany(  # sql-ok: identifier/column list from authority information_schema, values parameterized
            f'INSERT INTO "{table}" ({", ".join(names)}) '
            f'VALUES ({", ".join("?" for _ in names)})',
            converted,
        )


def export_postgresql_codex(target: Path) -> Path:
    """Export the live PostgreSQL codex authority to a SQLite scratch copy.

    The export is a non-authoritative working copy (A173): staged-generation
    tooling (amendment pipeline, mirror renderers, validation) operates on it
    without touching the authority, and the governed wire phase re-imports
    the staged file through ``import_sqlite_predecessor``.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    try:
        with readonly_connection() as source:
            for table in _authority_tables(source):
                _export_table(source, connection, table)
            connection.commit()
    finally:
        connection.close()
    return target


def _compare_table(
    sqlite_connection: sqlite3.Connection, target: Any, table: str
) -> tuple[int, bool]:
    """Return (checked rows, equal) for one predecessor table."""
    columns = [str(row[1]) for row in sqlite_connection.execute(f'PRAGMA table_info("{table}")')]  # sql-ok: table name derives from sqlite_master row set
    sqlite_rows = list(sqlite_connection.execute(f'SELECT * FROM "{table}"'))  # sql-ok: schema-agnostic full-table parity copy, identifier from predecessor catalog
    pg_rows = list(
        target.execute(
            sql.SQL("SELECT {} FROM {}.{}").format(
                sql.SQL(", ").join(sql.Identifier(name) for name in columns),
                sql.Identifier(CODEX_SCHEMA),
                sql.Identifier(table),
            )
        )
    )
    def _norm(row: Any) -> tuple[Any, ...]:
        return tuple(
            None if value is None else bytes(value) if isinstance(value, memoryview) else str(value)
            for value in row
        )
    return len(sqlite_rows), sorted(map(_norm, sqlite_rows), key=repr) == sorted(map(_norm, pg_rows), key=repr)


def verify_sqlite_parity(source: Path) -> dict[str, Any]:
    """Compare every predecessor table and cell with the live PostgreSQL authority."""
    source = Path(source).resolve()
    sqlite_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro&immutable=1", uri=True)
    mismatches: list[str] = []
    checked_rows = 0
    try:
        sqlite_tables = [
            str(row[0])
            for row in sqlite_connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        with readonly_connection() as target:
            pg_tables = _authority_tables(target)
            if sqlite_tables != pg_tables:
                mismatches.append("table-set")
            for table in sqlite_tables:
                count, equal = _compare_table(sqlite_connection, target, table)
                checked_rows += count
                if not equal:
                    mismatches.append(table)
        return {
            "result": "PASS" if not mismatches else "FAIL",
            "table_count": len(sqlite_tables),
            "row_count": checked_rows,
            "mismatches": mismatches,
        }
    finally:
        sqlite_connection.close()

"""PostgreSQL authority and one-way SQLite predecessor import for the Codex."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


CODEX_SCHEMA: Final[str] = "gptbridge_codex"
CODEX_AUTHORITY_URI: Final[str] = "postgresql://local/gptbridge_codex"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def runtime_dsn() -> str:
    value = os.environ.get("GPTBRIDGE_POSTGRES_DSN", "").strip()
    if not value:
        raise RuntimeError("GPTBRIDGE_POSTGRES_DSN_REQUIRED")
    return value


def admin_dsn() -> str:
    value = os.environ.get("GPTBRIDGE_POSTGRES_ADMIN_DSN", "").strip()
    if not value:
        raise RuntimeError("GPTBRIDGE_POSTGRES_ADMIN_DSN_REQUIRED")
    return value


@contextmanager
def readonly_connection() -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(runtime_dsn(), options="-c default_transaction_read_only=on") as connection:
        connection.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(CODEX_SCHEMA)))
        yield connection


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


def import_sqlite_predecessor(source: Path) -> dict[str, Any]:
    """Atomically replace the PostgreSQL Codex schema from the sealed predecessor."""
    source = Path(source).resolve()
    sqlite_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
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
        source_digest = _source_hash(source)
        source_version = str(metadata.get("codex_version", ""))
        total_rows = 0
        with psycopg.connect(admin_dsn()) as target:
            # Monotonic-version guard (fail-closed): the authority never
            # regresses.  A source older than the live codex version is a
            # stale/fixture import and must be refused before any DROP.
            try:
                row = target.execute(
                    sql.SQL(
                        "SELECT codex_version FROM {}.codex_authority_state"
                    ).format(sql.Identifier(CODEX_SCHEMA))
                ).fetchone()
            except psycopg.errors.Error:
                # Authority state unreadable (fresh init, missing
                # schema/table): nothing to regress against.
                row = None
                target.rollback()
            if row is not None:
                current_version = str(row[0])
                if _version_regresses(source_version, current_version):
                    raise RuntimeError(
                        "CODEX_VERSION_REGRESSION:"
                        f"{source_version}<{current_version}"
                    )
            target.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(CODEX_SCHEMA)))
            target.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(CODEX_SCHEMA)))
            target.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(CODEX_SCHEMA)))

            for table in tables:
                if not _IDENTIFIER.fullmatch(table):
                    raise RuntimeError(f"CODEX_TABLE_IDENTIFIER_INVALID:{table}")
                columns = list(sqlite_connection.execute(f'PRAGMA table_info("{table}")'))
                if not columns:
                    raise RuntimeError(f"CODEX_TABLE_SCHEMA_MISSING:{table}")
                rows = list(sqlite_connection.execute(f'SELECT * FROM "{table}"'))
                definitions: list[sql.Composable] = []
                pg_types: list[str] = []
                primary = sorted(((int(row[5]), str(row[1])) for row in columns if int(row[5])), key=lambda item: item[0])
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
                target.execute(
                    sql.SQL("CREATE TABLE {}.{} ({})").format(
                        sql.Identifier(CODEX_SCHEMA), sql.Identifier(table), sql.SQL(", ").join(definitions)
                    )
                )
                names = [str(row[1]) for row in columns]
                total_rows += len(rows)
                if rows:
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
                (CODEX_AUTHORITY_URI, str(metadata.get("codex_version", "")), source_digest, len(tables), total_rows),
            )
            target.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO gptbridge_runtime").format(sql.Identifier(CODEX_SCHEMA)))
            target.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO gptbridge_runtime").format(sql.Identifier(CODEX_SCHEMA)))
            target.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO gptbridge_runtime").format(sql.Identifier(CODEX_SCHEMA)))
        return {
            "codex_version": str(metadata.get("codex_version", "")),
            "source_sha256": source_digest,
            "table_count": len(tables),
            "row_count": total_rows,
        }
    finally:
        sqlite_connection.close()


def authority_state() -> dict[str, Any]:
    with readonly_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute(
                sql.SQL("SELECT * FROM {}.codex_authority_state").format(sql.Identifier(CODEX_SCHEMA))
            ).fetchone()
    if row is None:
        raise RuntimeError("POSTGRESQL_CODEX_AUTHORITY_NOT_INITIALIZED")
    return dict(row)


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


def export_postgresql_codex(target: Path) -> Path:
    """Export the live PostgreSQL codex authority to a SQLite scratch copy.

    The export is a non-authoritative working copy (A173): staged-generation
    tooling (amendment pipeline, mirror renderers, validation) operates on it
    without touching the authority, and the governed wire phase re-imports
    the staged file through :func:`import_sqlite_predecessor`.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    try:
        with readonly_connection() as source:
            tables = [
                str(row[0])
                for row in source.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_type='BASE TABLE' "
                    "AND table_name<>'codex_authority_state' ORDER BY table_name",
                    (CODEX_SCHEMA,),
                )
            ]
            for table in tables:
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
                connection.execute(
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
                    connection.executemany(
                        f'INSERT INTO "{table}" ({", ".join(names)}) '
                        f'VALUES ({", ".join("?" for _ in names)})',
                        converted,
                    )
            connection.commit()
    finally:
        connection.close()
    return target


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
            pg_tables = [
                str(row[0])
                for row in target.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_type='BASE TABLE' "
                    "AND table_name<>'codex_authority_state' ORDER BY table_name",
                    (CODEX_SCHEMA,),
                )
            ]
            if sqlite_tables != pg_tables:
                mismatches.append("table-set")
            for table in sqlite_tables:
                columns = [str(row[1]) for row in sqlite_connection.execute(f'PRAGMA table_info("{table}")')]
                sqlite_rows = list(sqlite_connection.execute(f'SELECT * FROM "{table}"'))
                pg_rows = list(
                    target.execute(
                        sql.SQL("SELECT {} FROM {}.{}").format(
                            sql.SQL(", ").join(sql.Identifier(name) for name in columns),
                            sql.Identifier(CODEX_SCHEMA),
                            sql.Identifier(table),
                        )
                    )
                )
                checked_rows += len(sqlite_rows)
                normalize = lambda row: tuple(None if value is None else bytes(value) if isinstance(value, memoryview) else str(value) for value in row)
                if sorted(map(normalize, sqlite_rows), key=repr) != sorted(map(normalize, pg_rows), key=repr):
                    mismatches.append(table)
        return {
            "result": "PASS" if not mismatches else "FAIL",
            "table_count": len(sqlite_tables),
            "row_count": checked_rows,
            "mismatches": mismatches,
        }
    finally:
        sqlite_connection.close()

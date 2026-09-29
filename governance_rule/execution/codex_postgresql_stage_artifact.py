"""Deterministic ``.sql`` artifact codec (A185 split).

Extracted from ``codex_postgresql_stage`` (source-size contract): the
serializable form of a staged generation — one statement per line,
``E''``-escaped literals so a line is always a complete statement.  Owns
``dump_schema`` (schema -> artifact), ``materialize_artifact``
(artifact -> schema) and the database-free readers
(``artifact_table_names`` / ``artifact_version``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from psycopg import sql as pg_sql

try:
    from .codex_postgresql_dsn import _IDENTIFIER
    from .codex_postgresql_stage_schema import (
        _admin,
        _schema_tables,
    )
except ImportError:  # flat script import (execution/ on sys.path)
    from codex_postgresql_dsn import _IDENTIFIER
    from codex_postgresql_stage_schema import (
        _admin,
        _schema_tables,
    )

ARTIFACT_HEADER = "-- gptbridge-codex-artifact/v1"


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "decode('" + bytes(value).hex() + "','hex')"
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return "E'" + escaped + "'"


def _primary_key_columns(connection: Any, schema: str, table: str) -> list[str]:
    rows = connection.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
        "WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY a.attnum",
        (f"{schema}.{table}",),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _artifact_type(pg_type: str) -> str:
    value = pg_type.upper()
    if "INT" in value or "SERIAL" in value:
        return "BIGINT"
    if any(token in value for token in ("DOUBLE", "REAL", "FLOA", "NUMERIC")):
        return "DOUBLE PRECISION"
    if "BYTEA" in value or "BLOB" in value:
        return "BYTEA"
    if value.startswith("TIMESTAMP"):
        return "TIMESTAMPTZ"
    if "BOOL" in value:
        return "BOOLEAN"
    return "TEXT"


def _dump_table(
    connection: Any, schema: str, table: str, lines: list[str]
) -> None:
    """Emit one table's CREATE TABLE + INSERT statements into ``lines``."""
    columns = [
        (str(row[0]), str(row[1]), str(row[2]) == "NO")
        for row in connection.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema=%s "
            "AND table_name=%s ORDER BY ordinal_position",
            (schema, table),
        )
    ]
    pk = _primary_key_columns(connection, schema, table)
    column_defs = [
        f'"{name}" {_artifact_type(pg_type)}{" NOT NULL" if notnull else ""}'
        for name, pg_type, notnull in columns
    ]
    if pk:
        column_defs.append(
            "PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk) + ")"
        )
    lines.append(f'CREATE TABLE "{table}" ({", ".join(column_defs)});')
    names = [name for name, _, _ in columns]
    order = ", ".join(f'"{c}"' for c in pk) if pk else ", ".join(
        f'"{c}"' for c in names
    )
    rows = connection.execute(  # sql-ok: bounded per-table export — catalog row set, deterministic ORDER BY
        pg_sql.SQL("SELECT {} FROM {}.{} ORDER BY {}").format(
            pg_sql.SQL(", ").join(pg_sql.Identifier(n) for n in names),
            pg_sql.Identifier(schema),
            pg_sql.Identifier(table),
            pg_sql.SQL(order),
        )
    ).fetchall()
    for row in rows:
        values = ", ".join(_literal(v) for v in row)
        lines.append(
            f'INSERT INTO "{table}" ({", ".join(chr(34)+n+chr(34) for n in names)}) '
            f"VALUES ({values});"
        )


def dump_schema(schema: str, target: Path, *, version: str | None = None) -> Path:
    """Write a deterministic ``.sql`` artifact for one schema."""
    if not _IDENTIFIER.fullmatch(schema):
        raise RuntimeError(f"STAGE_SCHEMA_NAME_INVALID:{schema}")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _admin() as connection:
        tables = _schema_tables(connection, schema)
        lines = [ARTIFACT_HEADER, f"-- source_schema: {schema}"]
        for table in tables:
            _dump_table(connection, schema, table, lines)
        lines.append("-- end-artifact")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def materialize_artifact(artifact: Path, schema: str) -> list[str]:
    """Create ``schema`` and execute one ``.sql`` artifact into it."""
    if not _IDENTIFIER.fullmatch(schema):
        raise RuntimeError(f"STAGE_SCHEMA_NAME_INVALID:{schema}")
    artifact = Path(artifact)
    raw_lines = artifact.read_text(encoding="utf-8").splitlines()
    statements = [
        line.strip()
        for line in raw_lines
        if line.strip() and not line.lstrip().startswith("--")
    ]
    with _admin() as connection:
        connection.execute(
            pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                pg_sql.Identifier(schema)
            )
        )
        connection.execute(
            pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema))
        )
        connection.execute(
            pg_sql.SQL("SET search_path TO {}, pg_catalog").format(
                pg_sql.Identifier(schema)
            )
        )
        for statement in statements:
            connection.execute(statement)  # sql-ok: governed artifact, line-per-statement codec
    return [s.split('"')[1] for s in statements if s.startswith('CREATE TABLE "')]


def artifact_table_names(artifact: Path) -> frozenset[str] | None:
    """Table names of a readable ``.sql`` artifact; None when unreadable."""
    try:
        text = Path(artifact).read_text(encoding="utf-8")
    except OSError:
        return None
    if ARTIFACT_HEADER not in text.splitlines()[:2]:
        return None
    return frozenset(
        re.findall(r'^CREATE TABLE "([^"]+)"', text, flags=re.M)
    )


def artifact_version(artifact: Path) -> str:
    """``metadata.codex_version`` parsed from the artifact without a database."""
    try:
        text = Path(artifact).read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(
        r'INSERT INTO "metadata" \([^)]*\) VALUES \(E?\'codex_version\', E?\'([^\']*)\'\)',
        text,
    )
    if match:
        return match.group(1).replace("\\'", "'").replace("\\\\", "\\")
    return ""

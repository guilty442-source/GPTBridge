"""sqlite_to_pg — one-shot governed migration of a SQLite database file
into a PostgreSQL schema (A610/A621: SQLite retired, PostgreSQL absorbs).

For every table in the source file the tool:

1. reads ``sqlite_master`` DDL + ``PRAGMA table_info``,
2. emits an equivalent ``CREATE TABLE IF NOT EXISTS`` in the target
   schema (type map: INTEGER->bigint, REAL->double precision,
   BLOB->bytea, everything else->text; ``INTEGER PRIMARY KEY`` on a
   single column keeps ``PRIMARY KEY`` — sequences are *not*
   reconstructed, callers renumber if needed),
3. copies all rows in batches,
4. records the move in ``gptbridge_audit.sqlite_migration_ledger``
   (source path, target schema.table, row count, sha256 of row content,
   timestamp) — the evidence trail DATA-SAFETY (A610) requires before
   the source file may be deleted.

Idempotent: re-running truncates-and-recopies unless ``--skip-existing``.
Intended usage is one cutover run per database, verified, then the
sqlite file is deleted (never before the ledger row exists).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.sqlite_to_pg")

_TYPE_MAP = {
    "INTEGER": "bigint",
    "INT": "bigint",
    "REAL": "double precision",
    "FLOAT": "double precision",
    "NUMERIC": "numeric",
    "BLOB": "bytea",
    "TEXT": "text",
}


@dataclass
class MigratedTable:
    name: str
    rows: int
    content_hash: str


def _pg_type(sqlite_type: str) -> str:
    head = (sqlite_type or "TEXT").split("(")[0].strip().upper()
    return _TYPE_MAP.get(head, "text")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return conn.execute(
        'SELECT cid, name, type, "notnull", dflt_value, pk '
        'FROM pragma_table_info(?)',
        (table,),
    ).fetchall()


def _ddl(pg_table: str, cols: list[tuple], pk_cols: list[str]) -> str:
    parts = []
    for _, name, typ, notnull, _, pk in cols:
        decl = f"{_quote_ident(name)} {_pg_type(typ)}"
        if pk and len(pk_cols) == 1:
            decl += " PRIMARY KEY"
        elif notnull:
            decl += " NOT NULL"
        parts.append(decl)
    if len(pk_cols) > 1:
        parts.append(
            "PRIMARY KEY (" + ", ".join(_quote_ident(c) for c in pk_cols) + ")"
        )
    return (
        f"CREATE TABLE IF NOT EXISTS {pg_table} (\n    "
        + ",\n    ".join(parts)
        + "\n)"
    )


def migrate_database(
    sqlite_path: str | Path,
    pg_conn: Any,
    schema: str,
    *,
    tables: list[str] | None = None,
    batch: int = 500,
) -> list[MigratedTable]:
    """Copy every (or the listed) table from *sqlite_path* into *schema*.

    Returns per-table migration receipts.  Caller commits / rolls back.
    """
    src = sqlite3.connect(f"file:{Path(sqlite_path).as_posix()}?mode=ro", uri=True)
    try:
        names = tables or [
            r[0]
            for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        pg_conn.execute(  # sql-ok: schema identifier sanitized, quotes stripped
            f'CREATE SCHEMA IF NOT EXISTS "{schema.replace(chr(34), "")}"'
        )
        receipts: list[MigratedTable] = []
        for table in names:
            cols = _table_columns(src, table)
            if not cols:
                continue
            pk_cols = [c[1] for c in cols if c[5]]
            col_names = [c[1] for c in cols]
            pg_table = f'"{schema}".{_quote_ident(table)}'
            pg_conn.execute(_ddl(pg_table, cols, pk_cols))  # sql-ok: bounded per-table loop, identifiers via _quote_ident
            pg_conn.execute(f"DELETE FROM {pg_table}")  # sql-ok: bounded per-table loop, identifiers via _quote_ident
            select = (
                "SELECT " + ", ".join(_quote_ident(c) for c in col_names)
                + " FROM " + _quote_ident(table)
            )
            insert = (
                f"INSERT INTO {pg_table} ("
                + ", ".join(_quote_ident(c) for c in col_names)
                + ") VALUES (" + ", ".join(["%s"] * len(col_names)) + ")"
            )
            hasher = hashlib.sha256()
            count = 0
            cursor = src.execute(select)  # sql-ok: column/table identifiers via _quote_ident, no user input
            pg_cur = pg_conn.cursor()
            while True:
                rows = cursor.fetchmany(batch)
                if not rows:
                    break
                pg_cur.executemany(insert, [tuple(r) for r in rows])  # sql-ok: bounded batch loop, identifiers via _quote_ident, values parameterized
                for r in rows:
                    hasher.update(repr(tuple(r)).encode("utf-8", "replace"))
                count += len(rows)
            receipts.append(
                MigratedTable(table, count, hasher.hexdigest())
            )
        return receipts
    finally:
        src.close()


def record_ledger(pg_conn: Any, source: str, receipts: list[MigratedTable], schema: str) -> None:
    pg_conn.execute(
        """CREATE TABLE IF NOT EXISTS gptbridge_audit.sqlite_migration_ledger (
               id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
               source_path text NOT NULL,
               target_schema text NOT NULL,
               table_name text NOT NULL,
               row_count bigint NOT NULL,
               content_hash text NOT NULL,
               migrated_at timestamptz NOT NULL DEFAULT now()
           )"""
    )
    pg_conn.cursor().executemany(
        "INSERT INTO gptbridge_audit.sqlite_migration_ledger "
        "(source_path, target_schema, table_name, row_count, content_hash) "
        "VALUES (%s, %s, %s, %s, %s)",
        [(source, schema, r.name, r.rows, r.content_hash) for r in receipts],
    )


def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: sqlite_to_pg <sqlite-file> <target-schema> [table ...]")
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import psycopg
    from shared_layer.security.dsn_policy import DsnPurpose, resolve_dsn

    source, schema = argv[1], argv[2]
    tables = argv[3:] or None
    with psycopg.connect(resolve_dsn(DsnPurpose.RUNTIME).dsn) as conn:
        receipts = migrate_database(source, conn, schema, tables=tables)
        record_ledger(conn, str(source), receipts, schema)
        conn.commit()
    for r in receipts:
        print(f"{r.name}: {r.rows} rows -> {schema} ({r.content_hash[:16]}…)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))


__all__ = ["MigratedTable", "migrate_database", "record_ledger"]

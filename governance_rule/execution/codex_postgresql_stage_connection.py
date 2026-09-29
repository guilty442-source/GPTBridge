"""sqlite3-shaped facade over a psycopg stage schema (A185 split).

Extracted from ``codex_postgresql_stage`` (source-size contract):
:class:`StageConnection` translates the codex pipeline's bounded sqlite
dialect — ``?`` placeholders, ``sqlite_master`` catalog reads, ``PRAGMA
table_info``/``integrity_check``/``quick_check``/``foreign_key_check``/
``database_list``, ``INSERT OR REPLACE``/``INSERT OR IGNORE``,
``CREATE VIRTUAL TABLE ... USING fts5`` and ``ORDER BY rowid`` — while
``open_stage`` / ``open_artifact`` / ``open_codex_store`` bind it to a
stage schema, a ``.sql`` artifact or the live authority.

Everything here is fail-closed: no DSN, no schema, no mutation of the live
authority (writes only ever land inside ``*_stage_*`` schemas).
"""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import sql as pg_sql

try:
    from .codex_postgresql_dsn import _IDENTIFIER, admin_dsn
    from .codex_postgresql_stage_schema import (
        drop_schema,
        sweep_stale_stage_schemas,
        unique_stage_name,
    )
    from .codex_postgresql_stage_artifact import (
        _primary_key_columns,
        dump_schema,
        materialize_artifact,
    )
except ImportError:  # flat script import (execution/ on sys.path)
    from codex_postgresql_dsn import _IDENTIFIER, admin_dsn
    from codex_postgresql_stage_schema import (
        drop_schema,
        sweep_stale_stage_schemas,
        unique_stage_name,
    )
    from codex_postgresql_stage_artifact import (
        _primary_key_columns,
        dump_schema,
        materialize_artifact,
    )

_PRAGMA_TABLE_INFO = re.compile(
    r"^\s*PRAGMA\s+table_info\s*\(\s*['\"]?([\w$]+)['\"]?\s*\)\s*;?\s*$", re.I
)
_PRAGMA_DATABASE_LIST = re.compile(r"^\s*PRAGMA\s+database_list\s*;?\s*$", re.I)
_PRAGMA_FK_CHECK = re.compile(
    r"^\s*PRAGMA\s+foreign_key_check\s*;?\s*$", re.I
)
_PRAGMA_INTEGRITY = re.compile(
    r"^\s*PRAGMA\s+(integrity_check|quick_check)\s*;?\s*$", re.I
)
_PRAGMA_OTHER = re.compile(r"^\s*PRAGMA\b", re.I)
_INSERT_OR_REPLACE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\b", re.I)
_INSERT_OR_IGNORE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b", re.I)
_VIRTUAL_FTS = re.compile(
    r"^\s*CREATE\s+VIRTUAL\s+TABLE\s+([\w$]+)\s+USING\s+fts5\s*\(([^)]*)\)",
    re.I,
)


def _ddl_pragma_translation(text: str, schema: str) -> str | None:
    """Final SQL for the statement-level rewrites, else ``None``.

    Covers the ``CREATE VIRTUAL TABLE ... fts5`` shim and every ``PRAGMA``
    probe — statements that resolve to a fixed replacement and never reach
    the placeholder/upsert phases.
    """
    virtual = _VIRTUAL_FTS.match(text)
    if virtual:
        name, columns = virtual.group(1), virtual.group(2)
        column_defs = ", ".join(
            f'"{column.strip()}" TEXT' for column in columns.split(",")
        )
        return f'CREATE TABLE "{name}" ({column_defs})'
    table_info = _PRAGMA_TABLE_INFO.match(text)
    if table_info:
        table = table_info.group(1)
        return (
            "SELECT ordinal_position - 1 AS cid, column_name AS name, "
            "data_type AS type, CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END "
            "AS notnull, column_default AS dflt_value, "
            "CASE WHEN column_name IN (SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid=i.indrelid AND "
            "a.attnum=ANY(i.indkey) WHERE i.indrelid="
            f"to_regclass(quote_ident(current_schema())||'.'||quote_ident('{table}')) "
            "AND i.indisprimary) THEN 1 ELSE 0 END AS pk "
            f"FROM information_schema.columns WHERE table_schema=current_schema() "
            f"AND table_name='{table}' ORDER BY ordinal_position"
        )
    if _PRAGMA_FK_CHECK.match(text):
        return "__fk_check__"
    if _PRAGMA_INTEGRITY.match(text):
        return "__integrity_ok__"
    if _PRAGMA_DATABASE_LIST.match(text):
        return f"SELECT 'main', '{schema}', '{schema}'"
    if _PRAGMA_OTHER.match(text):
        return "SELECT 1 WHERE false"
    return None


def _rewrite_sqlite_master(text: str) -> str:
    """Map ``sqlite_master`` catalog reads onto ``information_schema.tables``."""
    text = text.replace(
        "sqlite_master",
        "information_schema.tables",
    ).replace("name", "table_name")
    if "type='table'" in text:
        text = text.replace(
            "type='table'",
            "table_type='BASE TABLE' AND table_schema=current_schema()",
        )
    return text


class _StageCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)


class StageConnection:
    """sqlite3-compatible surface bound to one PostgreSQL stage schema."""

    def __init__(self, schema: str, *, readonly: bool = False):
        if not _IDENTIFIER.fullmatch(schema):
            raise RuntimeError(f"STAGE_SCHEMA_NAME_INVALID:{schema}")
        self.schema = schema
        self._connection = psycopg.connect(
            admin_dsn(), autocommit=False, connect_timeout=10
        )
        self._connection.execute(
            pg_sql.SQL("SET search_path TO {}, pg_catalog").format(
                pg_sql.Identifier(schema)
            )
        )
        self._readonly = readonly
        # Truthy marker → rows come back as dicts (sqlite3.Row-style
        # column-name access for migrated readers).
        self.row_factory: object | None = None

    # -- dialect translation -------------------------------------------------

    def _translate(self, statement: str) -> str:
        text = statement
        ddl = _ddl_pragma_translation(text, self.schema)
        if ddl is not None:
            return ddl
        if "sqlite_master" in text:
            text = _rewrite_sqlite_master(text)
        text = re.sub(r"ORDER\s+BY\s+rowid\b", "ORDER BY 1", text, flags=re.I)
        for pattern in (_INSERT_OR_REPLACE, _INSERT_OR_IGNORE):
            if pattern.match(text):
                text = pattern.sub("INSERT INTO", text)
                pk = self._primary_keys_for_statement(text)
                if pk:
                    target = ", ".join(f'"{c}"' for c in pk)
                    text += f" ON CONFLICT ({target}) DO NOTHING"
                break
        return self._placeholders(text)

    def _placeholders(self, statement: str) -> str:
        # SQLite NULL-safe equality (``"col" IS ?``) is invalid PostgreSQL
        # syntax (``IS $1``); translate it to ``IS NOT DISTINCT FROM`` so
        # amendment updates work against a stage schema.
        text = re.sub(r"\bIS\s+\?", "IS NOT DISTINCT FROM ?", statement, flags=re.I)
        return re.sub(r"%(?![sbt])", "%%", text).replace("?", "%s")

    def _primary_keys_for_statement(self, statement: str) -> list[str]:
        match = re.search(r'INSERT\s+INTO\s+"?([\w$]+)"?', statement, re.I)
        if not match:
            return []
        try:
            return _primary_key_columns(
                self._connection, self.schema, match.group(1)
            )
        except psycopg.Error:
            return []

    # -- DB-API surface --------------------------------------------------------

    def _cursor(self):
        if self.row_factory:
            from psycopg.rows import dict_row

            return self._connection.cursor(row_factory=dict_row)
        return self._connection.cursor()

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> _StageCursor:
        translated = self._translate(statement)
        if translated == "__fk_check__":
            return _StageCursor(_ListCursor(self._foreign_key_violations()))
        if translated == "__integrity_ok__":
            return _StageCursor(_ListCursor([("ok",)]))
        with self._cursor() as cursor:
            cursor.execute(translated, parameters or None)
            rows = list(cursor.fetchall()) if cursor.description else []
            return _StageCursor(_ListCursor(rows, rowcount=cursor.rowcount))

    def executemany(
        self, statement: str, rows: Sequence[Sequence[Any]]
    ) -> _StageCursor:
        translated = self._translate(statement)
        with self._cursor() as cursor:
            cursor.executemany(translated, rows)
            return _StageCursor(cursor)

    def executescript(self, script: str) -> None:
        for chunk in script.split(";"):
            if chunk.strip():
                self._connection.execute(chunk)  # sql-ok: caller-composed DDL batch

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "StageConnection":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- integrity probes --------------------------------------------------------

    def _foreign_key_violations(self) -> list[tuple[str, ...]]:
        constraints = self._connection.execute(
            "SELECT con.conname, con.conrelid::regclass::text, "
            "con.confrelid::regclass::text, con.conkey, con.confkey "
            "FROM pg_constraint con JOIN pg_namespace n ON n.oid=con.connamespace "
            "WHERE con.contype='f' AND n.nspname=current_schema()"
        ).fetchall()
        violations: list[tuple[str, ...]] = []
        for fk_name, child, parent, conkey, confkey in constraints:
            child_cols = self._attnames(child, conkey)
            parent_cols = self._attnames(parent, confkey)
            if not child_cols or not parent_cols:
                continue
            join = " AND ".join(
                f'c."{cc}" IS NOT DISTINCT FROM p."{pc}"'
                for cc, pc in zip(child_cols, parent_cols)
            )
            key_repr = "|| '|' ||".join(
                f"COALESCE(c.\"{cc}\"::text,'')" for cc in child_cols
            )
            rows = self._connection.execute(  # sql-ok: identifiers from pg_constraint catalog
                f'SELECT \'{child}\', {key_repr}, \'{fk_name}\', \'{parent}\' '
                f'FROM "{child.split(".")[-1]}" c WHERE NOT ('
                f'SELECT 1 FROM "{parent.split(".")[-1]}" p WHERE {join})'
            ).fetchall()
            violations.extend(tuple(str(v) for v in row) for row in rows)
        return violations

    def _attnames(self, relation: str, attnums: Sequence[int]) -> list[str]:
        rows = self._connection.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass "
            "AND attnum=ANY(%s) ORDER BY attnum",
            (relation, list(attnums)),
        ).fetchall()
        return [str(row[0]) for row in rows]


class _ListCursor:
    def __init__(self, rows: list, rowcount: int | None = None):
        self._rows = rows
        self.rowcount = len(rows) if rowcount is None or rowcount < 0 else rowcount
        self._index = 0

    def fetchone(self):
        if self._index < len(self._rows):
            row = self._rows[self._index]
            self._index += 1
            return row
        return None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


# ---------------------------------------------------------------------------
# Artifact-bound connections (the historical ``database: Path`` surface)
# ---------------------------------------------------------------------------


def open_stage(schema: str, *, readonly: bool = False) -> StageConnection:
    return StageConnection(schema, readonly=readonly)


@contextmanager
def open_artifact(
    artifact: Path | str, *, write_back: bool = False
) -> Iterator[StageConnection]:
    """Materialize a ``.sql`` artifact into a throwaway stage schema.

    ``write_back=True`` dumps the (mutated) schema back over the artifact
    before the schema is dropped — the artifact remains the single
    serializable form of the staged generation.
    """
    artifact_path = Path(artifact)
    token = hashlib.sha256(
        str(artifact_path.resolve()).encode("utf-8")
    ).hexdigest()[:16]
    # Per-open unique schema: a deterministic name let concurrent openers of
    # one artifact DROP each other's in-flight materialization (parallel
    # audit checks failed closed on ``relation does not exist``).
    schema = unique_stage_name(f"oa{token[:4]}")
    sweep_stale_stage_schemas()
    materialize_artifact(artifact_path, schema)
    connection = StageConnection(schema)
    try:
        yield connection
        if write_back:
            connection.commit()
            dump_schema(schema, artifact_path)
    finally:
        connection.close()
        drop_schema(schema)


@contextmanager
def open_codex_store(
    database: Path | str | None, *, write_back: bool = False
) -> Iterator[Any]:
    """Open a codex store — ``.sql`` artifact path or the live authority.

    ``None`` (or a path that is not a ``.sql`` artifact) reads the live
    PostgreSQL authority through the governed read-only repository
    connection; a ``.sql`` artifact materializes into a stage schema.
    """
    path = Path(database) if database is not None else None
    if path is not None and path.suffix == ".sql" and path.is_file():
        with open_artifact(path, write_back=write_back) as connection:
            yield connection
        return
    try:
        from .codex_repository import codex_readonly_connection
    except ImportError:  # flat script import
        from codex_repository import codex_readonly_connection
    with codex_readonly_connection() as connection:
        yield connection

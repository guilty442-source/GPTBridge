"""Export/verify paths between the PostgreSQL authority and ``.sql`` artifacts.

Extracted from ``codex_postgresql`` (source-size contract): owns the
non-authoritative staged-export and the cell-level parity verifier.  The
staged working copy is a PostgreSQL ``*_stage_*`` schema; the serializable
artifact is a deterministic ``.sql`` dump (``codex_postgresql_stage`` codec).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from psycopg import sql

try:
    from .codex_postgresql_dsn import _IDENTIFIER, CODEX_SCHEMA, admin_dsn
    from .codex_postgresql_pool import readonly_connection
    from .codex_postgresql_stage import (
        clone_authority,
        drop_schema,
        dump_schema,
        materialize_artifact,
        unique_stage_name,
    )
except ImportError:  # flat script import (execution/ on sys.path)
    from codex_postgresql_dsn import _IDENTIFIER, CODEX_SCHEMA, admin_dsn
    from codex_postgresql_pool import readonly_connection
    from codex_postgresql_stage import (
        clone_authority,
        drop_schema,
        dump_schema,
        materialize_artifact,
        unique_stage_name,
    )


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


def export_postgresql_codex(target: Path) -> Path:
    """Export the live PostgreSQL codex authority to a ``.sql`` artifact.

    The export is a non-authoritative snapshot (A173): staged-generation
    tooling (amendment pipeline, mirror renderers, validation) operates on a
    materialized staging schema without touching the authority, and the
    governed wire phase re-imports the staged artifact through
    ``import_codex_artifact``.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(
        f"export:{target.resolve()}".encode("utf-8")
    ).hexdigest()[:16]
    # Unique per invocation — a deterministic name let concurrent exports of
    # one target DROP each other's in-flight stage schema.
    stage = unique_stage_name(f"export{token[:4]}")
    try:
        clone_authority(stage)
        _drop_authority_state(stage)
        dump_schema(stage, target)
    finally:
        drop_schema(stage)
    return target


def _drop_authority_state(stage: str) -> None:
    """Remove the authority bookkeeping table from the staged snapshot."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(admin_dsn(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP TABLE IF EXISTS {}.codex_authority_state").format(
                sql.Identifier(stage)
            )
        )


def _table_columns(connection: Any, schema: str, table: str) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table),
        )
    ]


def _compare_table(
    source_connection: Any, source_schema: str, target: Any, table: str
) -> tuple[int, bool]:
    """Return (checked rows, equal) for one staged table vs the authority."""
    columns = _table_columns(source_connection, source_schema, table)
    stage_rows = list(
        source_connection.execute(
            sql.SQL("SELECT {} FROM {}.{}").format(
                sql.SQL(", ").join(sql.Identifier(name) for name in columns),
                sql.Identifier(source_schema),
                sql.Identifier(table),
            )
        )
    )
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

    return len(stage_rows), sorted(map(_norm, stage_rows), key=repr) == sorted(
        map(_norm, pg_rows), key=repr
    )


def _stage_token(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]


def verify_sql_parity(source: Path) -> dict[str, Any]:
    """Compare every artifact table and cell with the live PostgreSQL authority."""
    import psycopg  # noqa: PLC0415

    source = Path(source).resolve()
    # Unique per invocation — concurrent parity checks of one artifact must
    # not share (and recursively drop) one stage schema.
    stage = unique_stage_name(f"parity{_stage_token(source)[:4]}")
    materialize_artifact(source, stage)
    mismatches: list[str] = []
    checked_rows = 0
    stage_tables: list[str] = []
    try:
        with psycopg.connect(admin_dsn(), autocommit=True) as stage_connection:
            stage_tables = [
                str(row[0])
                for row in stage_connection.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_type='BASE TABLE' "
                    "ORDER BY table_name",
                    (stage,),
                )
            ]
            with readonly_connection() as target:
                pg_tables = _authority_tables(target)
                if stage_tables != pg_tables:
                    mismatches.append("table-set")
                for table in stage_tables:
                    count, equal = _compare_table(
                        stage_connection, stage, target, table
                    )
                    checked_rows += count
                    if not equal:
                        mismatches.append(table)
        return {
            "result": "PASS" if not mismatches else "FAIL",
            "table_count": len(stage_tables),
            "row_count": checked_rows,
            "mismatches": mismatches,
        }
    finally:
        drop_schema(stage)


__all__ = [
    "export_postgresql_codex",
    "verify_sql_parity",
]

"""``.sql`` artifact import into the PostgreSQL Codex authority.

Extracted from ``codex_postgresql`` (source-size contract): owns the
atomic schema-replace transaction — advisory-lock serialization,
monotonic-version regression guard, statement execution, and the
authority-state bookkeeping row.  The predecessor artifact is a
deterministic ``.sql`` dump (``codex_postgresql_stage`` codec).
"""

from __future__ import annotations

import hashlib
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
    from .codex_postgresql_stage import (
        ARTIFACT_HEADER,
        artifact_table_names,
        artifact_version,
    )
except ImportError:  # flat script import (execution/ on sys.path)
    from codex_postgresql_dsn import (
        _IDENTIFIER,
        CODEX_AUTHORITY_URI,
        CODEX_SCHEMA,
        admin_dsn,
    )
    from codex_postgresql_stage import (
        ARTIFACT_HEADER,
        artifact_table_names,
        artifact_version,
    )


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


def _read_predecessor(artifact: Path) -> tuple[list[str], dict[str, Any]]:
    """Return (table names, metadata map) from the sealed ``.sql`` artifact."""
    tables = artifact_table_names(artifact)
    if tables is None or "metadata" not in tables:
        raise RuntimeError("CODEX_SQL_ARTIFACT_INVALID")
    metadata: dict[str, Any] = {}
    version = artifact_version(artifact)
    if version:
        metadata["codex_version"] = version
    return sorted(tables), metadata


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


def _artifact_statements(artifact: Path) -> list[str]:
    raw_lines = Path(artifact).read_text(encoding="utf-8").splitlines()
    if not raw_lines or raw_lines[0].strip() != ARTIFACT_HEADER:
        raise RuntimeError("CODEX_SQL_ARTIFACT_HEADER_INVALID")
    return [
        line.strip()
        for line in raw_lines
        if line.strip() and not line.lstrip().startswith("--")
    ]


def import_codex_artifact(source: Path) -> dict[str, Any]:
    """Atomically replace the PostgreSQL Codex schema from the ``.sql`` artifact."""
    source = Path(source).resolve()
    tables, metadata = _read_predecessor(source)
    statements = _artifact_statements(source)
    source_digest = _source_hash(source)
    source_version = str(metadata.get("codex_version", ""))
    total_rows = 0
    with psycopg.connect(admin_dsn()) as target:
        _guard_import(target, source_version)
        target.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(CODEX_SCHEMA)))
        target.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(CODEX_SCHEMA)))
        target.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(CODEX_SCHEMA)))
        target.execute(
            sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                sql.Identifier(CODEX_SCHEMA)
            )
        )
        for statement in statements:
            if statement.startswith("INSERT INTO"):
                total_rows += 1
            target.execute(statement)  # sql-ok: governed artifact codec, line-per-statement
        _seal_authority(target, metadata, source_digest, len(tables), total_rows)
    return {
        "codex_version": str(metadata.get("codex_version", "")),
        "source_sha256": source_digest,
        "table_count": len(tables),
        "row_count": total_rows,
    }


__all__ = [
    "import_codex_artifact",
]

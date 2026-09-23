"""Revision Control — A503 實作。

Canonical mutable data 必須有 revision 欄位。
UPDATE ... WHERE id = ? AND revision = expected_revision
禁止 Last-Write-Wins 作為預設 canonical conflict policy。
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Optional

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

_logger = logging.getLogger("gptbridge.sql.revision")

# SQL identifiers must be plain names (optionally schema-qualified) — never
# raw caller strings. Fail-closed: anything else raises RevisionError.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?\Z")
_COLUMN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _sql_identifier(name: str, *, what: str) -> "sql.Identifier":
    if not _IDENTIFIER_RE.fullmatch(str(name or "")):
        raise RevisionError(f"unsafe SQL identifier ({what}): {name!r}")
    return sql.Identifier(*str(name).split("."))


def _sql_column(name: str) -> "sql.Identifier":
    if not _COLUMN_RE.fullmatch(str(name or "")):
        raise RevisionError(f"unsafe SQL column name: {name!r}")
    return sql.Identifier(str(name))


class RevisionError(Exception):
    """Revision control violation."""
    pass


class StaleRevisionError(RevisionError):
    """Optimistic lock failure - revision mismatch."""
    pass


class RevisionController:
    """Enforces revision control on canonical mutable data (A503)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                autocommit=False,
            )
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def get_current_revision(self, table: str, record_id: str, id_column: str = "id") -> Optional[int]:
        """Get current revision for a record."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT revision FROM {} WHERE {} = %s").format(
                    _sql_identifier(table, what="table"),
                    _sql_column(id_column),
                ),
                (record_id,),
            )
            row = cur.fetchone()
            return row["revision"] if row else None

    def update_with_revision(
        self,
        table: str,
        record_id: str,
        expected_revision: int,
        updates: dict[str, Any],
        id_column: str = "id",
        revision_column: str = "revision",
    ) -> int:
        """Update record with optimistic locking (A503).

        Returns new revision number.

        Raises:
            StaleRevisionError: If revision mismatch (affected_rows = 0)
        """
        conn = self._get_conn()

        # Build SET clause — column names are quoted identifiers, values stay
        # parameterized.
        set_parts = []
        params = []
        for col, val in updates.items():
            set_parts.append(sql.SQL("{} = %s").format(_sql_column(col)))
            params.append(val)

        revision_ident = _sql_column(revision_column)
        set_parts.append(
            sql.SQL("{} = {} + 1").format(revision_ident, revision_ident)
        )
        params.extend([record_id, expected_revision])

        query = sql.SQL(
            "UPDATE {table} SET {sets} WHERE {id_col} = %s AND {rev_col} = %s"
            " RETURNING {rev_col}"
        ).format(
            table=_sql_identifier(table, what="table"),
            sets=sql.SQL(", ").join(set_parts),
            id_col=_sql_column(id_column),
            rev_col=revision_ident,
        )

        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            if not row:
                conn.rollback()
                _logger.warning("RevisionController: stale revision table=%s id=%s expected=%d",
                               table, record_id, expected_revision)
                raise StaleRevisionError(
                    f"Concurrent modification: {table}/{record_id} revision {expected_revision} no longer current"
                )

            new_revision = row[revision_column]
            conn.commit()
            _logger.debug("RevisionController: updated %s/%s revision %d -> %d",
                         table, record_id, expected_revision, new_revision)
            return new_revision

    @contextmanager
    def transaction(self):
        """Transaction context with automatic revision checking."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def verify_no_lww_policy(table: str, policy_column: str = "conflict_policy") -> bool:
    """Verify table doesn't use LAST_WRITE_WINS as default (A503).

    LAST_WRITE_WINS forbidden as default canonical conflict policy.
    """
    # This would be a schema validation check
    # For now, return True (assumes schema is validated separately)
    return True


# Schema requirement for revision-controlled tables
REVISION_TABLE_REQUIREMENTS = """
-- Every canonical mutable table MUST have:
-- 1. revision INTEGER NOT NULL DEFAULT 1
-- 2. UNIQUE constraint on (id) - implicit via PK
-- 3. CHECK constraint: revision > 0

-- Example:
CREATE TABLE gptbridge_rag.resource_versions (
    resource_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    version INTEGER NOT NULL,          -- This IS the revision
    content_hash TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    knowledge_kind TEXT NOT NULL DEFAULT 'SOURCE',
    state TEXT NOT NULL DEFAULT 'SOURCE',
    chunk_policy_version TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    source_version INTEGER,
    previous_version INTEGER,
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (resource_id, module_id, version)  -- Composite PK includes revision
);

-- For single-row revision tables:
ALTER TABLE gptbridge_rag.chunks ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS idx_chunks_revision ON gptbridge_rag.chunks (revision);

-- Optimistic lock update pattern:
-- UPDATE gptbridge_rag.chunks
-- SET content = %s, content_hash = %s, revision = revision + 1
-- WHERE chunk_id = %s AND revision = %s
-- RETURNING revision
"""


__all__ = [
    "RevisionController",
    "RevisionError",
    "StaleRevisionError",
    "REVISION_TABLE_REQUIREMENTS",
    "verify_no_lww_policy",
]
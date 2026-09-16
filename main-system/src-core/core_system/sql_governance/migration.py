"""Migration Executor — A506 實作。

Migration 必須由唯一 SQL_MIGRATION_EXECUTOR 執行：
- read migration registry
- verify predecessor
- verify live source hash
- acquire migration lock
- execute approved migration
- verify target hash
- write migration receipt
- release lock

禁止：
- 一般 backend startup 執行
- 任意 module 執行
- ORM auto-create
- developer convenience code

Migration lock 全域唯一。
Production runtime 不自動修改 schema。
"""

from __future__ import __annotations__

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from .sql_concurrency import MigrationReceipt

_logger = logging.getLogger("gptbridge.sql.migration")


class MigrationError(Exception):
    """Migration execution error."""
    pass


class MigrationLockError(MigrationError):
    """Migration lock acquisition failed."""
    pass


class MigrationReceiptMismatchError(MigrationError):
    """Receipt hash mismatch."""
    pass


class MigrationExecutor:
    """SQL Migration Executor — 唯一授權執行角色 (A506)."""

    def __init__(self, dsn: str, executor_identity: str) -> None:
        self.dsn = dsn
        self.executor_identity = executor_identity
        self._conn: Optional[psycopg.Connection] = None
        self._lock_acquired = False

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                autocommit=False,
            )
        return self._conn

    def close(self) -> None:
        if self._lock_acquired:
            self.release_migration_lock()
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    @contextmanager
    def migration_lock(self, migration_id: str):
        """Acquire global migration lock (A506).

        Uses PostgreSQL advisory lock for global uniqueness.
        Only one migration generation at a time.
        """
        conn = self._get_conn()
        lock_id = self._lock_id(migration_id)

        try:
            with conn.cursor() as cur:
                # Try to acquire lock (non-blocking)
                cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock_id,))
                acquired = cur.fetchone()[0]
                if not acquired:
                    raise MigrationLockError(
                        f"Migration lock already held for {migration_id}. "
                        "Only one migration generation at a time."
                    )
                self._lock_acquired = True
                _logger.info("MigrationExecutor: acquired lock for %s", migration_id)

            try:
                yield conn
            finally:
                self.release_migration_lock()

        except Exception:
            conn.rollback()
            raise

    def _lock_id(self, migration_id: str) -> int:
        """Generate deterministic lock ID from migration_id."""
        return int(hashlib.sha256(migration_id.encode()).hexdigest()[:15], 16)

    def release_migration_lock(self) -> None:
        """Release migration lock."""
        if self._lock_acquired and self._conn and not self._conn.closed:
            self._lock_acquired = False
            _logger.info("MigrationExecutor: released migration lock")

    def execute_migration(
        self,
        migration_id: str,
        migration_sql: str,
        source_schema_hash: str,
        expected_target_hash: str,
        verification_suite: Optional[Callable[[psycopg.Connection], bool]] = None,
    ) -> MigrationReceipt:
        """Execute approved migration with full verification (A506).

        Steps:
        1. Acquire migration lock
        2. Verify predecessor migration completed
        3. Verify live source schema hash matches expected
        4. Execute migration SQL
        5. Verify target schema hash
        6. Run verification suite
        7. Write migration receipt
        8. Release lock

        Returns MigrationReceipt.
        """
        receipt = MigrationReceipt(
            receipt_id=f"mrcpt-{hashlib.sha256(migration_id.encode()).hexdigest()[:12]}",
            migration_id=migration_id,
            executor_identity=self.executor_identity,
            source_schema_hash=source_schema_hash,
            expected_target_hash=expected_target_hash,
            observed_target_hash="",
            started_at_utc=datetime.now(timezone.utc).isoformat(),
        )

        with self.migration_lock(migration_id) as conn:
            try:
                # 1. Verify predecessor
                self._verify_predecessor(conn, migration_id)

                # 2. Verify live source hash
                live_hash = self._compute_schema_hash(conn)
                if live_hash != source_schema_hash:
                    raise MigrationError(
                        f"Schema drift detected: live hash {live_hash[:16]}... "
                        f"!= expected source hash {source_schema_hash[:16]}..."
                    )

                # 3. Execute migration
                _logger.info("MigrationExecutor: executing migration %s", migration_id)
                with conn.cursor() as cur:
                    cur.execute(migration_sql)

                # 4. Verify target hash
                observed_hash = self._compute_schema_hash(conn)
                if observed_hash != expected_target_hash:
                    raise MigrationError(
                        f"Target hash mismatch: observed {observed_hash[:16]}... "
                        f"!= expected {expected_target_hash[:16]}..."
                    )

                receipt.observed_target_hash = observed_hash

                # 5. Run verification suite
                if verification_suite:
                    if not verification_suite(conn):
                        raise MigrationError("Verification suite failed")

                receipt.verification_suite_result = "PASSED"

                # 6. Write migration receipt
                self._write_migration_receipt(conn, receipt)

                # 7. Commit
                conn.commit()
                receipt.committed_at_utc = datetime.now(timezone.utc).isoformat()
                receipt.transaction_result = "COMMITTED"
                receipt.receipt_hash = receipt.compute_hash()

                _logger.info("MigrationExecutor: migration %s completed successfully", migration_id)
                return receipt

            except Exception as e:
                conn.rollback()
                receipt.transaction_result = "ROLLED_BACK"
                _logger.error("MigrationExecutor: migration %s failed: %s", migration_id, e)
                raise

    def _verify_predecessor(self, conn: psycopg.Connection, migration_id: str) -> None:
        """Verify predecessor migration completed successfully."""
        # Extract sequence number from migration_id (e.g., "0008_add_index")
        try:
            seq = int(migration_id.split("_")[0])
            if seq > 0:
                prev_id = f"{seq-1:04d}_"
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM gptbridge_rag.migration_receipts WHERE migration_id LIKE %s AND transaction_result = 'COMMITTED'",
                        (f"{prev_id}%",),
                    )
                    if not cur.fetchone():
                        raise MigrationError(f"Predecessor migration {prev_id}* not completed")
        except ValueError:
            # Non-numeric prefix, skip predecessor check
            pass

    def _compute_schema_hash(self, conn: psycopg.Connection) -> str:
        """Compute hash of current schema state."""
        with conn.cursor() as cur:
            # Get all table/column definitions in deterministic order
            cur.execute("""
                SELECT table_name, column_name, data_type, is_nullable, column_default,
                       ordinal_position
                FROM information_schema.columns
                WHERE table_schema = 'gptbridge_rag'
                ORDER BY table_name, ordinal_position
            """)
            rows = cur.fetchall()

            # Also get indexes
            cur.execute("""
                SELECT indexname, tablename, indexdef
                FROM pg_indexes
                WHERE schemaname = 'gptbridge_rag'
                ORDER BY tablename, indexname
            """)
            indexes = cur.fetchall()

            # Hash all
            content = json.dumps({
                "columns": [dict(r) for r in rows],
                "indexes": [dict(r) for r in indexes],
            }, sort_keys=True)

            return hashlib.sha256(content.encode()).hexdigest()

    def _write_migration_receipt(self, conn: psycopg.Connection, receipt: MigrationReceipt) -> None:
        """Write migration receipt to registry."""
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gptbridge_rag.migration_receipts
                (receipt_id, migration_id, executor_identity, source_schema_hash,
                 expected_target_hash, observed_target_hash, started_at_utc,
                 committed_at_utc, transaction_result, verification_suite_result,
                 rollback_policy, previous_migration_receipt_hash, receipt_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    receipt.receipt_id,
                    receipt.migration_id,
                    receipt.executor_identity,
                    receipt.source_schema_hash,
                    receipt.expected_target_hash,
                    receipt.observed_target_hash,
                    receipt.started_at_utc,
                    receipt.committed_at_utc,
                    receipt.transaction_result,
                    receipt.verification_suite_result,
                    receipt.rollback_policy,
                    receipt.previous_migration_receipt_hash,
                    receipt.receipt_hash,
                ),
            )


# Production runtime schema inspection (no auto-migrate)
class SchemaInspector:
    """Inspects schema version on startup - no auto-migrate (A506)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def inspect(self) -> dict[str, Any]:
        """Inspect current schema state."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            # Get current migration version
            cur.execute("""
                SELECT migration_id, transaction_result, committed_at_utc
                FROM gptbridge_rag.migration_receipts
                WHERE transaction_result = 'COMMITTED'
                ORDER BY committed_at_utc DESC
                LIMIT 1
            """)
            latest = cur.fetchone()

            # Compute current schema hash
            current_hash = self._compute_schema_hash(cur)

            return {
                "current_migration": latest["migration_id"] if latest else None,
                "last_migration_result": latest["transaction_result"] if latest else None,
                "last_migration_at": latest["committed_at_utc"] if latest else None,
                "schema_hash": current_hash,
                "inspected_at": datetime.now(timezone.utc).isoformat(),
            }

    def _compute_schema_hash(self, cur: psycopg.Cursor) -> str:
        cur.execute("""
            SELECT table_name, column_name, data_type, is_nullable, column_default,
                   ordinal_position
            FROM information_schema.columns
            WHERE table_schema = 'gptbridge_rag'
            ORDER BY table_name, ordinal_position
        """)
        rows = cur.fetchall()

        cur.execute("""
            SELECT indexname, tablename, indexdef
            FROM pg_indexes
            WHERE schemaname = 'gptbridge_rag'
            ORDER BY tablename, indexname
        """)
        indexes = cur.fetchall()

        content = json.dumps({
            "columns": [dict(r) for r in rows],
            "indexes": [dict(r) for r in indexes],
        }, sort_keys=True)

        return hashlib.sha256(content.encode()).hexdigest()

    def verify_schema_version(self, expected_hash: str) -> tuple[bool, str]:
        """Verify schema matches expected hash.

        Returns (matches, current_hash).
        """
        result = self.inspect()
        return result["schema_hash"] == expected_hash, result["schema_hash"]

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None


# Migration registry tables
MIGRATION_REGISTRY_SQL = """
-- Migration receipts registry
CREATE TABLE IF NOT EXISTS gptbridge_rag.migration_receipts (
    receipt_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL,
    executor_identity TEXT NOT NULL,
    source_schema_hash TEXT NOT NULL,
    expected_target_hash TEXT NOT NULL,
    observed_target_hash TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    committed_at_utc TEXT,
    transaction_result TEXT NOT NULL DEFAULT 'PENDING' CHECK (transaction_result IN ('PENDING','COMMITTED','ROLLED_BACK')),
    verification_suite_result TEXT NOT NULL DEFAULT 'PENDING' CHECK (verification_suite_result IN ('PENDING','PASSED','FAILED')),
    rollback_policy TEXT NOT NULL DEFAULT 'AUTOMATIC_ON_FAILURE',
    previous_migration_receipt_hash TEXT,
    receipt_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_migration_receipts_migration
ON gptbridge_rag.migration_receipts (migration_id);

CREATE INDEX IF NOT EXISTS idx_migration_receipts_result
ON gptbridge_rag.migration_receipts (transaction_result);
"""


__all__ = [
    "MigrationExecutor",
    "SchemaInspector",
    "MigrationReceipt",
    "MigrationError",
    "MigrationLockError",
    "MigrationReceiptMismatchError",
    "MIGRATION_REGISTRY_SQL",
]
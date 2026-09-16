"""Idempotency Framework — A502 實作。

所有可重試的 write 必須冪等：
- operation_id
- idempotency_key
- UNIQUE(owner_identity, operation_class, idempotency_key)
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import psycopg
from psycopg.rows import dict_row

from .sql_concurrency import SqlFault, FAULT_RETRY_CLASSIFICATION

_logger = logging.getLogger("gptbridge.sql.idempotency")


class IdempotencyError(Exception):
    """Idempotency violation."""
    pass


class IdempotencyCollisionError(IdempotencyError):
    """Duplicate idempotency key."""
    pass


class IdempotencyRegistry:
    """Manages idempotency keys with UNIQUE constraint (A502)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                autocommit=True,  # Idempotency checks need their own transaction
            )
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def create_idempotency_record(
        self,
        owner_identity: str,
        operation_class: str,
        idempotency_key: str,
        operation_id: str,
        payload: Optional[dict] = None,
    ) -> str:
        """Create idempotency record. Returns existing receipt if duplicate.

        UNIQUE(owner_identity, operation_class, idempotency_key) enforced by DB.
        """
        conn = self._get_conn()
        receipt_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO gptbridge_rag.idempotency_registry
                    (receipt_id, owner_identity, operation_class, idempotency_key,
                     operation_id, payload, status, created_at, completed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        receipt_id,
                        owner_identity,
                        operation_class,
                        idempotency_key,
                        operation_id,
                        psycopg.types.json.Json(payload) if payload else None,
                        "PENDING",
                        now,
                        None,
                    ),
                )
            _logger.debug("IdempotencyRegistry: created record key=%s receipt=%s",
                         idempotency_key, receipt_id)
            return receipt_id

        except psycopg.errors.UniqueViolation:
            # Fetch existing receipt
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT receipt_id, operation_id, payload, status, created_at, completed_at
                    FROM gptbridge_rag.idempotency_registry
                    WHERE owner_identity = %s AND operation_class = %s AND idempotency_key = %s
                    """,
                    (owner_identity, operation_class, idempotency_key),
                )
                row = cur.fetchone()
                if row:
                    _logger.info("IdempotencyRegistry: duplicate key=%s returning existing receipt=%s",
                                idempotency_key, row["receipt_id"])
                    raise IdempotencyCollisionError(
                        f"Duplicate idempotency key: {idempotency_key}. "
                        f"Existing receipt: {row['receipt_id']}, status: {row['status']}"
                    )
            raise

    def mark_completed(self, receipt_id: str, result: Any = None) -> bool:
        """Mark idempotency record as completed."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.idempotency_registry
                SET status = 'COMPLETED', result = %s, completed_at = %s
                WHERE receipt_id = %s
                """,
                (psycopg.types.json.Json(result) if result else None,
                 datetime.now(timezone.utc).isoformat(),
                 receipt_id),
            )
            return cur.rowcount > 0

    def mark_failed(self, receipt_id: str, error: str) -> bool:
        """Mark idempotency record as failed."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.idempotency_registry
                SET status = 'FAILED', error = %s, completed_at = %s
                WHERE receipt_id = %s
                """,
                (error, datetime.now(timezone.utc).isoformat(), receipt_id),
            )
            return cur.rowcount > 0

    def get_receipt(self, owner_identity: str, operation_class: str, idempotency_key: str) -> Optional[dict]:
        """Get existing receipt for duplicate detection."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT receipt_id, operation_id, payload, status, created_at, completed_at, result, error
                FROM gptbridge_rag.idempotency_registry
                WHERE owner_identity = %s AND operation_class = %s AND idempotency_key = %s
                """,
                (owner_identity, operation_class, idempotency_key),
            )
            return cur.fetchone()

    @contextmanager
    def idempotent_operation(
        self,
        owner_identity: str,
        operation_class: str,
        idempotency_key: str,
        operation_id: Optional[str] = None,
    ):
        """Context manager for idempotent operation.

        Usage:
            with registry.idempotent_operation("user123", "payment", "pay-abc") as receipt:
                if receipt["status"] == "COMPLETED":
                    return receipt["result"]  # Return cached result
                # Do work
                result = do_payment()
                # On success, mark completed
        """
        op_id = operation_id or f"op-{uuid.uuid4().hex[:12]}"
        receipt_id = uuid.uuid4().hex[:16]

        try:
            receipt_id = self.create_idempotency_record(
                owner_identity=owner_identity,
                operation_class=operation_class,
                idempotency_key=idempotency_key,
                operation_id=op_id,
            )
            yield {"receipt_id": receipt_id, "status": "PENDING", "operation_id": op_id}
        except IdempotencyCollisionError as e:
            # Fetch and return existing
            existing = self.get_receipt(owner_identity, operation_class, idempotency_key)
            if existing:
                yield dict(existing)
            else:
                raise


# Table schema for idempotency registry
IDEMPOTENCY_REGISTRY_SQL = """
CREATE TABLE IF NOT EXISTS gptbridge_rag.idempotency_registry (
    receipt_id TEXT PRIMARY KEY,
    owner_identity TEXT NOT NULL,
    operation_class TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    payload JSONB,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','COMPLETED','FAILED')),
    result JSONB,
    error TEXT,
    created_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    UNIQUE (owner_identity, operation_class, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_registry_lookup
ON gptbridge_rag.idempotency_registry (owner_identity, operation_class, idempotency_key);

CREATE INDEX IF NOT EXISTS idx_idempotency_registry_status
ON gptbridge_rag.idempotency_registry (status);
"""


def generate_idempotency_key(
    owner_identity: str,
    operation_class: str,
    *args: Any,
) -> str:
    """Generate deterministic idempotency key from owner + class + parameters."""
    import hashlib
    content = f"{owner_identity}:{operation_class}:{':'.join(str(a) for a in args)}"
    return hashlib.sha256(content.encode()).hexdigest()[:32]


__all__ = [
    "IdempotencyRegistry",
    "IdempotencyError",
    "IdempotencyCollisionError",
    "IDEMPOTENCY_REGISTRY_SQL",
    "generate_idempotency_key",
]
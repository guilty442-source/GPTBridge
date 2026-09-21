"""Connection Pool Security — A504 實作。

Pool 僅供資源重用，必須不保留：
- requester authorization
- previous transaction context
- previous session variables
- role escalation
- temporary security state

Connection 回 pool 前必須 deterministic reset。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Optional, Literal

import psycopg
from psycopg.pool import ConnectionPool

_logger = logging.getLogger("gptbridge.sql.pool")


class PoolSecurityError(Exception):
    """Pool security violation."""
    pass


class SessionLeakError(PoolSecurityError):
    """Session state leak detected."""
    pass


@dataclass
class PoolMetrics:
    """Connection pool health metrics."""
    pool_size: int = 0
    active_connections: int = 0
    idle_connections: int = 0
    waiting_requests: int = 0
    checkout_timeout_count: int = 0
    oldest_transaction_age_seconds: float = 0.0
    long_running_transaction_count: int = 0
    aborted_transaction_count: int = 0
    connection_reset_failures: int = 0
    idle_in_transaction_count: int = 0


class SecureConnectionPool:
    """Connection pool with deterministic reset (A504)."""

    def __init__(
        self,
        dsn: str,
        min_size: int = 2,
        max_size: int = 10,
        reset_role: bool = True,
        reset_search_path: bool = True,
        reset_temp_settings: bool = True,
        reset_request_identity: bool = True,
        reset_correlation_id: bool = True,
        reset_authorization_context: bool = True,
    ) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.reset_role = reset_role
        self.reset_search_path = reset_search_path
        self.reset_temp_settings = reset_temp_settings
        self.reset_request_identity = reset_request_identity
        self.reset_correlation_id = reset_correlation_id
        self.reset_authorization_context = reset_authorization_context

        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            configure=self._configure_connection,
        )
        self._metrics = PoolMetrics()

    def _configure_connection(self, conn: psycopg.Connection) -> None:
        """Configure connection on creation."""
        conn.autocommit = False
        # Set default search_path
        conn.execute("SET search_path = gptbridge_rag, public")

    @contextmanager
    def connection(self):
        """Get connection with deterministic reset on return."""
        conn = self._pool.getconn()
        self._metrics.active_connections += 1
        self._metrics.idle_connections = max(0, self._metrics.idle_connections - 1)

        try:
            yield conn
        except Exception as e:
            # On error, ensure rollback before return
            try:
                conn.rollback()
            except Exception:
                pass
            self._metrics.aborted_transaction_count += 1
            raise
        finally:
            # A504: Deterministic reset before returning to pool
            self._reset_connection(conn)
            self._pool.putconn(conn)
            self._metrics.active_connections -= 1
            self._metrics.idle_connections += 1

    def _reset_connection(self, conn: psycopg.Connection) -> None:
        """Deterministic connection reset (A504).

        Must clear:
        - transaction (rollback)
        - role (RESET ROLE)
        - search_path (RESET search_path)
        - temporary session settings (RESET ALL)
        - request identity (CLEAR app.* variables)
        - correlation id
        - authorization context
        """
        try:
            # 1. Ensure no open transaction
            if not conn.autocommit:
                conn.rollback()

            # 2. Reset role
            conn.execute("RESET ROLE")

            # 3. Reset search_path
            conn.execute("SET search_path = gptbridge_rag, public")

            # 4. Reset temporary settings
            conn.execute("RESET ALL")

            # 5. Clear application session variables
            conn.execute("SET app.requester_identity = ''")
            conn.execute("SET app.requester_generation = '0'")
            conn.execute("SET app.active_role_identity = ''")
            conn.execute("SET app.permission_generation = '0'")
            conn.execute("SET app.correlation_id = ''")
            conn.execute("SET app.operation_id = ''")
            conn.execute("SET app.authorized = 'false'")

            # 6. Ensure autocommit off for next use
            conn.autocommit = False

            _logger.debug("SecureConnectionPool: connection reset completed")

        except Exception as e:
            _logger.error("SecureConnectionPool: reset failed: %s", e)
            self._metrics.connection_reset_failures += 1
            raise SessionLeakError(f"Connection reset failed: {e}")

    def get_metrics(self) -> PoolMetrics:
        """Get pool health metrics."""
        # Update dynamic metrics
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    # Pool size
                    cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
                    self._metrics.pool_size = cur.fetchone()[0]

                    # Active/idle
                    cur.execute("""
                        SELECT
                            count(*) FILTER (WHERE state = 'active') as active,
                            count(*) FILTER (WHERE state = 'idle') as idle,
                            count(*) FILTER (WHERE state = 'idle in transaction') as idle_in_tx,
                            max(EXTRACT(EPOCH FROM (NOW() - xact_start))) FILTER (WHERE xact_start IS NOT NULL) as oldest_tx_age,
                            count(*) FILTER (WHERE state = 'active' AND xact_start < NOW() - INTERVAL '5 minutes') as long_running
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                    """)
                    row = cur.fetchone()
                    self._metrics.active_connections = row[0] or 0
                    self._metrics.idle_connections = row[1] or 0
                    self._metrics.idle_in_transaction_count = row[2] or 0
                    self._metrics.oldest_transaction_age_seconds = row[3] or 0.0
                    self._metrics.long_running_transaction_count = row[4] or 0

        except Exception as e:
            _logger.warning("SecureConnectionPool: metrics collection failed: %s", e)

        return self._metrics

    def check_idle_transactions(self) -> list[str]:
        """Detect idle-in-transaction connections (should be faults)."""
        violations = []
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT pid, usename, application_name, client_addr,
                               EXTRACT(EPOCH FROM (NOW() - xact_start)) as age_seconds
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND state = 'idle in transaction'
                          AND xact_start < NOW() - INTERVAL '30 seconds'
                    """)
                    for row in cur.fetchall():
                        violations.append(
                            f"PID {row[0]} ({row[1]}@{row[3]}) idle in transaction for {row[4]:.0f}s"
                        )
        except Exception as e:
            _logger.warning("SecureConnectionPool: idle transaction check failed: %s", e)
        return violations

    def close(self) -> None:
        """Close all connections."""
        self._pool.close()
        _logger.info("SecureConnectionPool: closed")

    @contextmanager
    def acquire_for_workload(
        self,
        workload_class: Literal["interactive", "transport", "audit", "reconciliation", "maintenance", "migration"],
    ):
        """Acquire a connection with workload-class timeouts applied (C4).

        Reads timeout settings from gptbridge_index.workload_class table
        and applies SET LOCAL statement_timeout / lock_timeout for the
        transaction scope. The connection is returned with deterministic reset.
        """
        conn = self._pool.getconn()
        self._metrics.active_connections += 1
        self._metrics.idle_connections = max(0, self._metrics.idle_connections - 1)

        try:
            # Fetch workload class timeouts
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT statement_timeout_ms, lock_timeout_ms FROM gptbridge_index.workload_class WHERE class_name = %s",
                    (workload_class,),
                )
                row = cur.fetchone()
                if row:
                    stmt_timeout_ms, lock_timeout_ms = row
                    conn.execute(f"SET LOCAL statement_timeout = '{stmt_timeout_ms}ms'")
                    conn.execute(f"SET LOCAL lock_timeout = '{lock_timeout_ms}ms'")
                    conn.execute(f"SET LOCAL gptbridge.workload_class = '{workload_class}'")

            yield conn
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            self._metrics.aborted_transaction_count += 1
            raise
        finally:
            self._reset_connection(conn)
            self._pool.putconn(conn)
            self._metrics.active_connections -= 1
            self._metrics.idle_connections += 1


class ThreeLayerIdentity:
    """Three-layer identity separation (A505).

    1. DATABASE_LOGIN_ROLE - Pool layer (shared, limited)
    2. APPLICATION_PRINCIPAL - Module/service layer (per-module)
    3. REQUESTER_IDENTITY - Business actor layer (per-request)
    """

    def __init__(
        self,
        database_login_role: str,
        application_principal: str,
        requester_identity: str,
    ) -> None:
        self.database_login_role = database_login_role
        self.application_principal = application_principal
        self.requester_identity = requester_identity

    def apply_to_connection(self, conn: psycopg.Connection) -> None:
        """Apply identity to connection session variables."""
        conn.execute(f"SET app.database_login_role = '{self.database_login_role}'")
        conn.execute(f"SET app.application_principal = '{self.application_principal}'")
        conn.execute(f"SET app.requester_identity = '{self.requester_identity}'")


__all__ = [
    "SecureConnectionPool",
    "PoolMetrics",
    "PoolSecurityError",
    "SessionLeakError",
    "ThreeLayerIdentity",
]
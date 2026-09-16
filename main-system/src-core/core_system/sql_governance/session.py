"""Session Binding — A505 實作。

DB Session 必須綁定 process-generation 身分：
- requester_identity
- requester_generation
- active_role_identity
- permission_generation
- correlation_id
- operation_id

Session variable 是 context，不是 authority。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

_logger = logging.getLogger("gptbridge.sql.session")


class SessionBindingError(Exception):
    """Session binding violation."""
    pass


@dataclass(frozen=True)
class SessionContext:
    """Complete session context for governance (A505)."""
    requester_identity: str
    requester_generation: int
    active_role_identity: str
    permission_generation: int
    correlation_id: str
    operation_id: str
    authorized: bool = False

    # Three-layer identity
    database_login_role: str = ""
    application_principal: str = ""

    def to_session_vars(self) -> dict[str, str]:
        """Convert to PostgreSQL session variables."""
        return {
            "app.requester_identity": self.requester_identity,
            "app.requester_generation": str(self.requester_generation),
            "app.active_role_identity": self.active_role_identity,
            "app.permission_generation": str(self.permission_generation),
            "app.correlation_id": self.correlation_id,
            "app.operation_id": self.operation_id,
            "app.authorized": str(self.authorized).lower(),
            "app.database_login_role": self.database_login_role,
            "app.application_principal": self.application_principal,
        }


class SessionBinder:
    """Binds session context to PostgreSQL connection (A505)."""

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

    @contextmanager
    def bind(self, context: SessionContext):
        """Bind session context to connection."""
        conn = self._get_conn()

        try:
            # Clear any existing session vars
            self._clear_session_vars(conn)

            # Set new context
            for key, value in context.to_session_vars().items():
                conn.execute(f"SET {key} = '{value}'")

            # Verify authorization if required
            if context.authorized:
                if not self._verify_authorization(conn, context):
                    raise SessionBindingError(
                        f"Authorization verification failed for {context.requester_identity}"
                    )

            yield conn

        finally:
            # Always clear on exit
            self._clear_session_vars(conn)

    def _clear_session_vars(self, conn: psycopg.Connection) -> None:
        """Clear all app.* session variables."""
        vars_to_clear = [
            "app.requester_identity",
            "app.requester_generation",
            "app.active_role_identity",
            "app.permission_generation",
            "app.correlation_id",
            "app.operation_id",
            "app.authorized",
            "app.database_login_role",
            "app.application_principal",
        ]
        for var in vars_to_clear:
            try:
                conn.execute(f"SET {var} = ''")
            except Exception:
                pass

    def _verify_authorization(self, conn: psycopg.Connection, context: SessionContext) -> bool:
        """Verify session authorization matches signed projection.

        Session variable 是 context，不是 authority。
        必須與 signed/validated authorization projection 相符。
        """
        try:
            with conn.cursor() as cur:
                # Check if requester has the claimed role in current generation
                cur.execute(
                    """
                    SELECT 1 FROM gptbridge_rag.permission_projection
                    WHERE subject_identity = %s
                      AND role_identity = %s
                      AND generation = %s
                      AND valid = true
                    """,
                    (context.requester_identity, context.active_role_identity, context.permission_generation),
                )
                result = cur.fetchone()
                return result is not None
        except Exception as e:
            _logger.warning("SessionBinder: authorization verification failed: %s", e)
            return False

    def get_current_context(self, conn: psycopg.Connection) -> Optional[SessionContext]:
        """Extract current session context from connection."""
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        current_setting('app.requester_identity', true) as requester_identity,
                        current_setting('app.requester_generation', true) as requester_generation,
                        current_setting('app.active_role_identity', true) as active_role_identity,
                        current_setting('app.permission_generation', true) as permission_generation,
                        current_setting('app.correlation_id', true) as correlation_id,
                        current_setting('app.operation_id', true) as operation_id,
                        current_setting('app.authorized', true) as authorized,
                        current_setting('app.database_login_role', true) as database_login_role,
                        current_setting('app.application_principal', true) as application_principal
                """)
                row = cur.fetchone()
                if row and row["requester_identity"]:
                    return SessionContext(
                        requester_identity=row["requester_identity"],
                        requester_generation=int(row["requester_generation"] or 0),
                        active_role_identity=row["active_role_identity"],
                        permission_generation=int(row["permission_generation"] or 0),
                        correlation_id=row["correlation_id"],
                        operation_id=row["operation_id"],
                        authorized=row["authorized"].lower() == "true",
                        database_login_role=row["database_login_role"],
                        application_principal=row["application_principal"],
                    )
        except Exception:
            pass
        return None


# PostgreSQL session variable setup (run once per DB)
SESSION_VARIABLE_SETUP_SQL = """
-- Create session variables for governance
DO $$
BEGIN
    -- Requester identity
    CREATE OR REPLACE FUNCTION app.requester_identity() RETURNS TEXT LANGUAGE SQL AS
    'SELECT current_setting(''app.requester_identity'', true)';

    -- Requester generation
    CREATE OR REPLACE FUNCTION app.requester_generation() RETURNS INTEGER LANGUAGE SQL AS
    'SELECT COALESCE(NULLIF(current_setting(''app.requester_generation'', true), '''')::INT, 0)';

    -- Active role identity
    CREATE OR REPLACE FUNCTION app.active_role_identity() RETURNS TEXT LANGUAGE SQL AS
    'SELECT current_setting(''app.active_role_identity'', true)';

    -- Permission generation
    CREATE OR REPLACE FUNCTION app.permission_generation() RETURNS INTEGER LANGUAGE SQL AS
    'SELECT COALESCE(NULLIF(current_setting(''app.permission_generation'', true), '''')::INT, 0)';

    -- Correlation ID
    CREATE OR REPLACE FUNCTION app.correlation_id() RETURNS TEXT LANGUAGE SQL AS
    'SELECT current_setting(''app.correlation_id'', true)';

    -- Operation ID
    CREATE OR REPLACE FUNCTION app.operation_id() RETURNS TEXT LANGUAGE SQL AS
    'SELECT current_setting(''app.operation_id'', true)';

    -- Authorized flag
    CREATE OR REPLACE FUNCTION app.authorized() RETURNS BOOLEAN LANGUAGE SQL AS
    'SELECT current_setting(''app.authorized'', true)::BOOLEAN';

    -- Database login role
    CREATE OR REPLACE FUNCTION app.database_login_role() RETURNS TEXT LANGUAGE SQL AS
    'SELECT current_setting(''app.database_login_role'', true)';

    -- Application principal
    CREATE OR REPLACE FUNCTION app.application_principal() RETURNS TEXT LANGUAGE SQL AS
    'SELECT current_setting(''app.application_principal'', true)';
END $$;
"""


# Security predicates using session variables
SECURITY_PREDICATE_EXAMPLES = """
-- Example: Row-level security using session variables
CREATE POLICY resource_access_policy ON gptbridge_rag.resources
    USING (
        module_id IN (
            SELECT module_id FROM gptbridge_rag.module_assignments
            WHERE subject_identity = app.requester_identity()
              AND generation = app.permission_generation()
              AND valid = true
        )
    );

-- Example: Audit trigger using session variables
CREATE OR REPLACE FUNCTION audit_log_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO gptbridge_rag.audit_log (
        operation_id, requester_identity, requester_generation,
        role_identity, permission_generation, correlation_id,
        table_name, operation, old_data, new_data, timestamp
    ) VALUES (
        app.operation_id(),
        app.requester_identity(),
        app.requester_generation(),
        app.active_role_identity(),
        app.permission_generation(),
        app.correlation_id(),
        TG_TABLE_NAME,
        TG_OP,
        to_jsonb(OLD),
        to_jsonb(NEW),
        NOW()
    );
    RETURN NEW;
END;
$$;
"""


__all__ = [
    "SessionBinder",
    "SessionContext",
    "SessionBindingError",
    "SESSION_VARIABLE_SETUP_SQL",
    "SECURITY_PREDICATE_EXAMPLES",
]
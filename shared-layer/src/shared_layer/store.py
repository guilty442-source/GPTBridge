"""store — governed PostgreSQL transport (codex A8/A44/A49 + E30/E35).

The shared-layer request channel is now backed by ````gptbridge_transport.tool_request````
in the PostgreSQL central index.  The implementation uses ````pg_notify```` for
channel-level alerts and ````SELECT ... FOR UPDATE SKIP LOCKED```` for safe
concurrent claim operations.

PostgreSQL is the sole structured-data authority (A610/A621); the
``LocalSharedLayerStore`` SQLite fallback was retired with the migration
window and ``SharedLayerStore`` is the PostgreSQL variant only.

Split layout (source-size contract): ``store_pool`` owns the bounded
connection pool; ``store_request_ops``/``store_claim_ops``/
``store_push_ops`` carry the operation mixins; this module keeps
construction, DSN resolution, authorization and session-identity binding.
"""

from __future__ import annotations

import os
import threading
import logging
from pathlib import Path
from typing import Any

from governance_rule.execution.authentication import (
    GovernanceAuthenticationService,
)
from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
)

from .store_async import PostgresStoreAsyncMixin
from .store_claim_ops import PostgresStoreClaimMixin
from .store_helpers import (
    _CHANS,
    _POOL_MAX_CONN,
    _POOL_MIN_CONN,
    _POOL_TIMEOUT,
    normalize_id as _id,
)
from .store_pool import PgConnectionPool
from .store_push_ops import PostgresStorePushMixin
from .store_request_ops import PostgresStoreRequestMixin


_logger = logging.getLogger("gptbridge.shared_layer.store")


class PostgresSharedLayerStore(
    PostgresStoreAsyncMixin,
    PostgresStoreRequestMixin,
    PostgresStoreClaimMixin,
    PostgresStorePushMixin,
):
    """Governed PostgreSQL transport backed by gptbridge_transport.tool_request.

    Channel subscribers may LISTEN on ````tool_request_<channel_id>````; new rows
    are announced with pg_notify.  Concurrent claim operations use FOR UPDATE
    SKIP LOCKED so multiple executors can safely share one table.
    """

    _pool: PgConnectionPool | None = None
    _pool_lock = threading.Lock()

    def __init__(
        self,
        project_root: Path | str,
        authentication: GovernanceAuthenticationService,
        channel_id: str = "system",
    ) -> None:
        if not isinstance(authentication, GovernanceAuthenticationService):
            raise permission_denied()
        self._authentication = authentication
        self._project_root = Path(project_root).resolve()
        self._channel_id = str(channel_id or "").strip().casefold()
        if self._channel_id not in _CHANS:
            raise permission_denied()
        policy = directory_authority_snapshot().shared_layer_access_policy
        declared = (
            policy.ai_database_path
            if self._channel_id == "ai"
            else policy.database_path
        )
        if not declared.startswith("postgresql:"):
            raise permission_denied()
        self._resource_path = declared
        self._dsn = self._build_dsn()
        self._ensure_pool()

    def _build_dsn(self) -> str:
        try:
            from psycopg.conninfo import conninfo_to_dict, make_conninfo

            values = conninfo_to_dict(os.environ.get("GPTBRIDGE_POSTGRES_DSN", ""))
            if not values.get("dbname"):
                values["dbname"] = "gptbridge"
            return make_conninfo(**values)
        except Exception as exc:
            raise permission_denied() from exc

    @classmethod
    def _ensure_pool(cls) -> None:
        if cls._pool is None:
            with cls._pool_lock:
                if cls._pool is None:
                    # DSN is needed; defer actual creation to first instance
                    pass

    def _get_pool(self) -> PgConnectionPool:
        if PostgresSharedLayerStore._pool is None:
            with PostgresSharedLayerStore._pool_lock:
                if PostgresSharedLayerStore._pool is None:
                    PostgresSharedLayerStore._pool = PgConnectionPool(
                        self._dsn, _POOL_MIN_CONN, _POOL_MAX_CONN, _POOL_TIMEOUT
                    )
        return PostgresSharedLayerStore._pool

    def _authorize(self, token: str, action: str, target_tool_id: str) -> str:
        claims = self._authentication.authenticate_token(token)
        capability = f"{self._channel_id}-channel-request-" + (
            "submit" if action in {"request", "cancel-request", "consume-response"} else "process"
        )
        policy = directory_authority_snapshot().shared_layer_access_policy
        valid_resource_path = claims.resource_path in {
            policy.database_path,
            policy.ai_database_path,
        }
        if (
            claims.capability != capability
            or claims.action != action
            or claims.target != f"shared-layer-{self._channel_id}-request:{target_tool_id}"
            or claims.data_scope != f"shared-layer-{self._channel_id}-request"
            or not valid_resource_path
        ):
            raise permission_denied()
        if action in {"request", "cancel-request", "consume-response"}:
            if claims.target_tool_id != target_tool_id and not (
                claims.target_tool_id is None
                and claims.bound_tool_id == target_tool_id
            ):
                raise permission_denied()
        elif claims.target_tool_id is not None or claims.bound_tool_id != target_tool_id:
            raise permission_denied()
        return claims.actor

    def _notify(self, connection: Any, request_id: str) -> None:
        connection.execute(
            "SELECT pg_notify(%s, %s)",
            (f"tool_request_{self._channel_id}", _id(request_id)),
        )

    def _bind_identity(
        self, connection: Any, actor: str, request_id: str = ""
    ) -> None:
        """Bind the short-term session identity to the current transaction.

        Every transport statement is attributed to the authenticated actor
        through ``set_config(..., local)`` (A501 session identity).  Binding is
        metadata: a failure is logged and never fails the transport itself.
        """
        try:
            from .security.session_identity import (
                SessionIdentity,
                apply_session_identity,
            )

            apply_session_identity(
                connection,
                SessionIdentity(
                    actor_id=str(actor or ""),
                    module_id=self._channel_id,
                    request_id=_id(request_id) if request_id else "",
                ),
            )
        except Exception:
            _logger.warning("session identity binding skipped", exc_info=True)


SharedLayerStore = PostgresSharedLayerStore

__all__ = [
    "PostgresSharedLayerStore",
    "SharedLayerStore",
]

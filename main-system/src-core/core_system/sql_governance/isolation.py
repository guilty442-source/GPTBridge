"""Isolation Level Enforcement by Resource Class — A501 實作。

資料類型決定最低隔離要求，法典禁止硬套全系統 SERIALIZABLE。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from .sql_concurrency import (
    ResourceClass,
    IsolationRequirement,
    RESOURCE_CLASS_ISOLATION,
)

_logger = logging.getLogger("gptbridge.sql.isolation")


class IsolationLevelError(Exception):
    """Isolation level violation."""
    pass


class IsolationEnforcer:
    """Enforces isolation level per resource class (A501)."""

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
    def with_isolation(self, resource_class: ResourceClass):
        """Execute with required isolation level for resource class."""
        conn = self._get_conn()
        isolation = RESOURCE_CLASS_ISOLATION.get(resource_class, IsolationRequirement.READ_COMMITTED)

        try:
            conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation.value}")
            _logger.debug("IsolationEnforcer: set isolation=%s for resource_class=%s",
                         isolation.value, resource_class.value)
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except psycopg.errors.SerializationFailure as e:
            conn.rollback()
            raise IsolationLevelError(f"Serialization failure at {isolation.value}: {e}")
        except Exception as e:
            conn.rollback()
            raise

    def get_required_isolation(self, resource_class: ResourceClass) -> IsolationRequirement:
        """Get required isolation for resource class."""
        return RESOURCE_CLASS_ISOLATION.get(resource_class, IsolationRequirement.READ_COMMITTED)


# Resource class detection helpers
def detect_resource_class(table_name: str, operation: str) -> "ResourceClass":
    """Detect resource class from table and operation."""
    from .sql_concurrency import ResourceClass

    # Security projections
    if any(kw in table_name.lower() for kw in ["permission", "identity", "role", "access", "authz"]):
        return ResourceClass.SECURITY_PROJECTION

    # Canonical official data
    if any(kw in table_name.lower() for kw in ["resource", "chunk", "generation", "index_state", "canonical"]):
        return ResourceClass.CENTRAL_OFFICIAL_DATA

    # Audit append
    if "audit" in table_name.lower() or "log" in table_name.lower():
        return ResourceClass.AUDIT_APPEND

    # Transport delivery
    if any(kw in table_name.lower() for kw in ["outbox", "inbox", "message", "delivery"]):
        return ResourceClass.TRANSPORT_DELIVERY

    # Derived cache
    if any(kw in table_name.lower() for kw in ["cache", "derived", "materialized"]):
        return ResourceClass.DERIVED_CACHE

    # Private state
    if any(kw in table_name.lower() for kw in ["preference", "ui_state", "session", "window"]):
        return ResourceClass.PRIVATE_STATE

    # Default: canonical official data
    return ResourceClass.CENTRAL_OFFICIAL_DATA


class IsolationPolicyRegistry:
    """Registry of isolation policies per table/operation."""

    def __init__(self) -> None:
        self._policies: dict[str, IsolationRequirement] = {}
        self._defaults = RESOURCE_CLASS_ISOLATION.copy()

    def register(self, table_pattern: str, isolation: IsolationRequirement) -> None:
        """Register explicit isolation requirement for table pattern."""
        self._policies[table_pattern] = isolation
        _logger.info("IsolationPolicyRegistry: registered %s -> %s", table_pattern, isolation.value)

    def get_isolation(self, table_name: str) -> IsolationRequirement:
        """Get isolation requirement for table."""
        # Check exact match first
        if table_name in self._policies:
            return self._policies[table_name]

        # Check pattern match
        for pattern, isolation in self._policies.items():
            if pattern.endswith("*") and table_name.startswith(pattern[:-1]):
                return isolation

        # Fallback to resource class detection
        resource_class = detect_resource_class(table_name, "")
        return self._defaults.get(resource_class, IsolationRequirement.READ_COMMITTED)


DEFAULT_ISOLATION_REGISTRY = IsolationPolicyRegistry()

# Register known tables
DEFAULT_ISOLATION_REGISTRY.register("gptbridge_rag.resource_versions", IsolationRequirement.SERIALIZABLE)
DEFAULT_ISOLATION_REGISTRY.register("gptbridge_rag.chunks", IsolationRequirement.REPEATABLE_READ)
DEFAULT_ISOLATION_REGISTRY.register("gptbridge_rag.index_state", IsolationRequirement.REPEATABLE_READ)
DEFAULT_ISOLATION_REGISTRY.register("gptbridge_rag.generations", IsolationRequirement.REPEATABLE_READ)
DEFAULT_ISOLATION_REGISTRY.register("gptbridge_rag.outbox_event", IsolationRequirement.READ_COMMITTED)
DEFAULT_ISOLATION_REGISTRY.register("gptbridge_rag.inbox_dedup", IsolationRequirement.READ_COMMITTED)
DEFAULT_ISOLATION_REGISTRY.register("gptbridge_rag.audit_*", IsolationRequirement.READ_COMMITTED)
DEFAULT_ISOLATION_REGISTRY.register("gptbridge_rag.migration_receipts", IsolationRequirement.SERIALIZABLE)
DEFAULT_ISOLATION_REGISTRY.register("gptbridge_rag.model_registry", IsolationRequirement.READ_COMMITTED)


__all__ = [
    "IsolationEnforcer",
    "IsolationLevelError",
    "ResourceClass",
    "IsolationRequirement",
    "RESOURCE_CLASS_ISOLATION",
    "detect_resource_class",
    "IsolationPolicyRegistry",
    "DEFAULT_ISOLATION_REGISTRY",
]
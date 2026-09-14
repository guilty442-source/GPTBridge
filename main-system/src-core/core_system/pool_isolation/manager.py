"""Pool Isolation — A368 Implementation.

A368: POOL-ISOLATION: central-index, transport, audit and each module-private database
use separately budgeted connection pools with owner,max/open/idle/wait limits.

This module enforces connection pool isolation with budgeted limits for:
- central-index (PostgreSQL central index)
- transport (information layer transport)
- audit (audit logging)
- each module-private database
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import psycopg
import psycopg.pool

_logger = logging.getLogger("gptbridge.pool_isolation")


class PoolOwner(Enum):
    """A368: Pool owners with separate budgets."""
    CENTRAL_INDEX = "central-index"
    TRANSPORT = "transport"
    AUDIT = "audit"
    MODULE_PRIVATE = "module-private"


@dataclass(frozen=True)
class PoolBudget:
    """A368: Budget limits for a connection pool."""
    owner: PoolOwner
    max_connections: int
    max_open: int
    max_idle: int
    wait_timeout_seconds: float
    module_id: Optional[str] = None  # For module-private pools


@dataclass
class PoolStats:
    """Runtime statistics for a pool."""
    owner: PoolOwner
    module_id: Optional[str]
    total_created: int = 0
    currently_open: int = 0
    currently_idle: int = 0
    wait_count: int = 0
    wait_timeouts: int = 0
    total_wait_time_seconds: float = 0.0
    last_error: Optional[str] = None
    last_error_time: Optional[str] = None


class IsolatedPool:
    """A single isolated connection pool with budget enforcement."""

    def __init__(self, budget: PoolBudget, dsn: str) -> None:
        self.budget = budget
        self.dsn = dsn
        self._pool: Optional[psycopg.pool.AsyncConnectionPool] = None
        self._stats = PoolStats(owner=budget.owner, module_id=budget.module_id)
        self._lock = asyncio.Lock()
        self._closed = False

    async def initialize(self) -> bool:
        """Initialize the connection pool."""
        try:
            self._pool = psycopg.pool.AsyncConnectionPool(
                self.dsn,
                min_size=0,
                max_size=self.budget.max_connections,
                open=False,
                timeout=self.budget.wait_timeout_seconds,
                kwargs={"autocommit": True},
            )
            await self._pool.open()
            await self._pool.wait()
            _logger.info("IsolatedPool: initialized for %s (max=%d)", self.budget.owner.value, self.budget.max_connections)
            return True
        except Exception as exc:
            _logger.error("IsolatedPool: failed to initialize for %s: %s", self.budget.owner.value, exc)
            self._stats.last_error = str(exc)
            self._stats.last_error_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return False

    async def acquire(self, timeout: Optional[float] = None) -> psycopg.AsyncConnection:
        """Acquire a connection from the pool with budget enforcement."""
        if self._closed:
            raise RuntimeError(f"Pool {self.budget.owner.value} is closed")

        wait_timeout = timeout or self.budget.wait_timeout_seconds
        start_time = time.monotonic()

        async with self._lock:
            # Check open connections limit
            if self._stats.currently_open >= self.budget.max_open:
                # Wait for a connection to become available
                wait_start = time.monotonic()
                while self._stats.currently_open >= self.budget.max_open:
                    if time.monotonic() - wait_start > self.budget.wait_timeout_seconds:
                        self._stats.wait_timeouts += 1
                        raise TimeoutError(f"Pool {self.budget.owner.value}: max open connections ({self.budget.max_open}) exceeded")
                    await asyncio.sleep(0.1)

            self._stats.currently_open += 1
            self._stats.wait_count += 1

        try:
            conn = await asyncio.wait_for(self._pool.getconn(), timeout=wait_timeout)
            self._stats.total_created += 1
            return conn
        except Exception as exc:
            async with self._lock:
                self._stats.currently_open -= 1
            self._stats.last_error = str(exc)
            self._stats.last_error_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            raise

    def release(self, conn: psycopg.AsyncConnection) -> None:
        """Release a connection back to the pool."""
        if self._closed:
            self._pool.putconn(conn, close=True)
            return

        try:
            self._pool.putconn(conn)
        except Exception:
            self._pool.putconn(conn, close=True)
        finally:
            self._stats.currently_open = max(0, self._stats.currently_open - 1)
            if self._stats.currently_idle < self.budget.max_idle:
                self._stats.currently_idle += 1
            else:
                # Too many idle, close the connection
                self._pool.putconn(conn, close=True)

    def get_stats(self) -> PoolStats:
        """Get current pool statistics."""
        return PoolStats(
            owner=self._stats.owner,
            module_id=self._stats.module_id,
            total_created=self._stats.total_created,
            currently_open=self._stats.currently_open,
            currently_idle=self._stats.currently_idle,
            wait_count=self._stats.wait_count,
            wait_timeouts=self._stats.wait_timeouts,
            total_wait_time_seconds=self._stats.total_wait_time_seconds,
            last_error=self._stats.last_error,
            last_error_time=self._stats.last_error_time,
        )

    async def close(self) -> None:
        """Close the pool and all connections."""
        self._closed = True
        if self._pool:
            await self._pool.close()
            self._pool = None
        _logger.info("IsolatedPool: closed for %s", self.budget.owner.value)


class PoolIsolationManager:
    """A368: Manages all isolated connection pools with budget enforcement.

    Manages pools for:
    - central-index (PostgreSQL central index)
    - transport (information layer transport)
    - audit (audit logging)
    - each module-private database
    """

    def __init__(self) -> None:
        self._pools: dict[str, IsolatedPool] = {}
        self._default_dsn: Optional[str] = None
        self._lock = asyncio.Lock()

    def set_default_dsn(self, dsn: str) -> None:
        """Set the default PostgreSQL DSN for pools."""
        self._default_dsn = dsn

    def create_pool(
        self,
        owner: PoolOwner,
        dsn: Optional[str] = None,
        max_connections: int = 10,
        max_open: int = 8,
        max_idle: int = 4,
        wait_timeout_seconds: float = 30.0,
        module_id: Optional[str] = None,
    ) -> IsolatedPool:
        """Create a new isolated pool with budget."""
        pool_dsn = dsn or self._default_dsn
        if not pool_dsn:
            raise ValueError("No DSN provided for pool")

        pool_key = f"{owner.value}:{module_id}" if module_id else owner.value

        budget = PoolBudget(
            owner=owner,
            max_connections=max_connections,
            max_open=max_open,
            max_idle=max_idle,
            wait_timeout_seconds=wait_timeout_seconds,
            module_id=module_id,
        )

        pool = IsolatedPool(budget, pool_dsn)
        self._pools[pool_key] = pool
        _logger.info("PoolIsolationManager: created pool %s (max=%d, open=%d, idle=%d)",
                     pool_key, max_connections, max_open, max_idle)
        return pool

    async def get_pool(self, owner: PoolOwner, module_id: Optional[str] = None) -> Optional[IsolatedPool]:
        """Get an existing pool."""
        pool_key = f"{owner.value}:{module_id}" if module_id else owner.value
        return self._pools.get(pool_key)

    async def initialize_pool(
        self,
        owner: PoolOwner,
        dsn: Optional[str] = None,
        max_connections: int = 10,
        max_open: int = 8,
        max_idle: int = 4,
        wait_timeout_seconds: float = 30.0,
        module_id: Optional[str] = None,
    ) -> bool:
        """Create and initialize a pool."""
        pool = self.create_pool(
            owner=owner,
            dsn=dsn,
            max_connections=max_connections,
            max_open=max_open,
            max_idle=max_idle,
            wait_timeout_seconds=wait_timeout_seconds,
            module_id=module_id,
        )
        return await pool.initialize()

    async def initialize_default_pools(self) -> dict[str, bool]:
        """Initialize the standard A368 pools with default budgets."""
        if not self._default_dsn:
            raise ValueError("Default DSN not set")

        results = {}

        # A368: central-index pool
        results["central-index"] = await self.initialize_pool(
            owner=PoolOwner.CENTRAL_INDEX,
            max_connections=20,
            max_open=15,
            max_idle=5,
            wait_timeout_seconds=30.0,
        )

        # A368: transport pool
        results["transport"] = await self.initialize_pool(
            owner=PoolOwner.TRANSPORT,
            max_connections=15,
            max_open=10,
            max_idle=3,
            wait_timeout_seconds=20.0,
        )

        # A368: audit pool
        results["audit"] = await self.initialize_pool(
            owner=PoolOwner.AUDIT,
            max_connections=5,
            max_open=3,
            max_idle=1,
            wait_timeout_seconds=10.0,
        )

        return results

    async def create_module_private_pool(
        self,
        module_id: str,
        dsn: Optional[str] = None,
        max_connections: int = 5,
        max_open: int = 3,
        max_idle: int = 1,
        wait_timeout_seconds: float = 15.0,
    ) -> IsolatedPool:
        """Create a module-private pool with budget."""
        pool = self.create_pool(
            owner=PoolOwner.MODULE_PRIVATE,
            dsn=dsn,
            max_connections=max_connections,
            max_open=max_open,
            max_idle=max_idle,
            wait_timeout_seconds=wait_timeout_seconds,
            module_id=module_id,
        )
        await pool.initialize()
        return pool

    def get_all_stats(self) -> dict[str, PoolStats]:
        """Get statistics for all pools."""
        return {key: pool.get_stats() for key, pool in self._pools.items()}

    async def close_all(self) -> None:
        """Close all pools."""
        for pool in self._pools.values():
            await pool.close()
        self._pools.clear()
        _logger.info("PoolIsolationManager: all pools closed")

    def get_pool_budgets(self) -> dict[str, dict[str, Any]]:
        """Get budget configuration for all pools."""
        result = {}
        for key, pool in self._pools.items():
            b = pool.budget
            result[key] = {
                "owner": b.owner.value,
                "module_id": b.module_id,
                "max_connections": b.max_connections,
                "max_open": b.max_open,
                "max_idle": b.max_idle,
                "wait_timeout_seconds": b.wait_timeout_seconds,
            }
        return result


# Default budgets per A368
DEFAULT_POOL_BUDGETS: dict[PoolOwner, dict[str, Any]] = {
    PoolOwner.CENTRAL_INDEX: {
        "max_connections": 20,
        "max_open": 15,
        "max_idle": 5,
        "wait_timeout_seconds": 30.0,
    },
    PoolOwner.TRANSPORT: {
        "max_connections": 15,
        "max_open": 10,
        "max_idle": 3,
        "wait_timeout_seconds": 20.0,
    },
    PoolOwner.AUDIT: {
        "max_connections": 5,
        "max_open": 3,
        "max_idle": 1,
        "wait_timeout_seconds": 10.0,
    },
    PoolOwner.MODULE_PRIVATE: {
        "max_connections": 5,
        "max_open": 3,
        "max_idle": 1,
        "wait_timeout_seconds": 15.0,
    },
}


def create_pool_manager_from_env() -> PoolIsolationManager:
    """Create PoolIsolationManager from environment variables."""
    manager = PoolIsolationManager()
    dsn = os.environ.get("POSTGRESQL_DSN")
    if dsn:
        manager.set_default_dsn(dsn)
    return manager


__all__ = [
    "PoolOwner",
    "PoolBudget",
    "PoolStats",
    "IsolatedPool",
    "PoolIsolationManager",
    "DEFAULT_POOL_BUDGETS",
    "create_pool_manager_from_env",
]
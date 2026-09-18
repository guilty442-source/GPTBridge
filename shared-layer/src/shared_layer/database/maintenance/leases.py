"""Maintenance Lease.

Scope-based lease management for maintenance actions.
Prevents conflicting actions on the same database/scope.
Supports acquire, renew, release, and expired lease recovery.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID, uuid4

from .models import MaintenanceJob


@dataclass(frozen=True)
class MaintenanceLease:
    """A maintenance lease record."""

    scope: str
    holder: str  # job_id or worker_id
    action_id: str
    generation: int
    claimed_at: datetime
    lease_until: datetime
    job_id: Optional[UUID] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, at: Optional[datetime] = None) -> bool:
        """Check if lease is expired."""
        check_time = at or datetime.utcnow()
        return check_time >= self.lease_until

    def remaining_seconds(self, at: Optional[datetime] = None) -> float:
        """Get remaining lease time in seconds."""
        check_time = at or datetime.utcnow()
        delta = self.lease_until - check_time
        return max(0.0, delta.total_seconds())


class LeaseConflictError(Exception):
    """Raised when lease acquisition fails due to conflict."""

    def __init__(self, scope: str, existing_holder: str, existing_until: datetime) -> None:
        self.scope = scope
        self.existing_holder = existing_holder
        self.existing_until = existing_until
        super().__init__(
            f"Lease conflict on {scope}: held by {existing_holder} until {existing_until}"
        )


class LeaseManager:
    """Thread-safe lease manager with in-memory state.

    For production, leases should be persisted to PostgreSQL.
    This implementation provides the interface; persistence is handled
    by the controller via job persistence.
    """

    def __init__(self, default_ttl_seconds: float = 300.0) -> None:
        self._leases: dict[str, MaintenanceLease] = {}
        self._lock = threading.RLock()
        self._default_ttl = default_ttl_seconds

    def acquire(
        self,
        scope: str,
        holder: str,
        action_id: str,
        generation: int,
        ttl_seconds: Optional[float] = None,
        job_id: Optional[UUID] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> MaintenanceLease:
        """Acquire a lease for the given scope.

        Args:
            scope: Lease scope (e.g., "pg:analyze:mydb")
            holder: Unique holder identifier (worker ID or job ID)
            action_id: Maintenance action ID
            generation: Current system generation
            ttl_seconds: Lease TTL (defaults to default_ttl_seconds)
            job_id: Optional associated job ID
            metadata: Optional metadata

        Returns:
            The acquired lease

        Raises:
            LeaseConflictError: If scope is already leased by another holder
        """
        ttl = ttl_seconds or self._default_ttl
        now = datetime.utcnow()
        lease_until = now + timedelta(seconds=ttl)

        with self._lock:
            existing = self._leases.get(scope)
            if existing and not existing.is_expired(now):
                if existing.holder != holder:
                    raise LeaseConflictError(scope, existing.holder, existing.lease_until)
                # Same holder - renew instead
                return self.renew(scope, holder, ttl_seconds=ttl)

            lease = MaintenanceLease(
                scope=scope,
                holder=holder,
                action_id=action_id,
                generation=generation,
                claimed_at=now,
                lease_until=lease_until,
                job_id=job_id,
                metadata=metadata or {},
            )
            self._leases[scope] = lease
            return lease

    def renew(
        self,
        scope: str,
        holder: str,
        ttl_seconds: Optional[float] = None,
    ) -> MaintenanceLease:
        """Renew an existing lease.

        Args:
            scope: Lease scope
            holder: Current holder (must match)
            ttl_seconds: New TTL

        Returns:
            The renewed lease

        Raises:
            LeaseConflictError: If lease doesn't exist or holder mismatch
        """
        ttl = ttl_seconds or self._default_ttl
        now = datetime.utcnow()
        lease_until = now + timedelta(seconds=ttl)

        with self._lock:
            existing = self._leases.get(scope)
            if not existing:
                raise LeaseConflictError(scope, "none", now)
            if existing.holder != holder:
                raise LeaseConflictError(scope, existing.holder, existing.lease_until)

            renewed = MaintenanceLease(
                scope=existing.scope,
                holder=existing.holder,
                action_id=existing.action_id,
                generation=existing.generation,
                claimed_at=existing.claimed_at,
                lease_until=lease_until,
                job_id=existing.job_id,
                metadata=existing.metadata,
            )
            self._leases[scope] = renewed
            return renewed

    def release(self, scope: str, holder: str) -> bool:
        """Release a lease.

        Args:
            scope: Lease scope
            holder: Current holder (must match)

        Returns:
            True if released, False if not held by holder
        """
        with self._lock:
            existing = self._leases.get(scope)
            if not existing or existing.holder != holder:
                return False
            del self._leases[scope]
            return True

    def get(self, scope: str) -> Optional[MaintenanceLease]:
        """Get current lease for scope."""
        with self._lock:
            lease = self._leases.get(scope)
            if lease and lease.is_expired():
                return None
            return lease

    def list_active(self) -> list[MaintenanceLease]:
        """List all active (non-expired) leases."""
        now = datetime.utcnow()
        with self._lock:
            return [l for l in self._leases.values() if not l.is_expired(now)]

    def list_expired(self, at: Optional[datetime] = None) -> list[MaintenanceLease]:
        """List all expired leases."""
        check_time = at or datetime.utcnow()
        with self._lock:
            return [l for l in self._leases.values() if l.is_expired(check_time)]

    def recover_expired(self, at: Optional[datetime] = None) -> list[MaintenanceLease]:
        """Recover (remove) expired leases.

        Returns:
            List of recovered leases
        """
        check_time = at or datetime.utcnow()
        with self._lock:
            expired = [l for l in self._leases.values() if l.is_expired(check_time)]
            for lease in expired:
                del self._leases[lease.scope]
            return expired

    def force_release(self, scope: str) -> bool:
        """Force release a lease (admin operation)."""
        with self._lock:
            if scope in self._leases:
                del self._leases[scope]
                return True
            return False


# Global lease manager instance
_lease_manager: Optional[LeaseManager] = None


def get_lease_manager() -> LeaseManager:
    """Get the global lease manager."""
    global _lease_manager
    if _lease_manager is None:
        _lease_manager = LeaseManager()
    return _lease_manager


def acquire_lease(
    scope: str,
    holder: str,
    action_id: str,
    generation: int,
    ttl_seconds: Optional[float] = None,
    job_id: Optional[UUID] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> MaintenanceLease:
    """Acquire a lease using the global manager."""
    return get_lease_manager().acquire(
        scope, holder, action_id, generation, ttl_seconds, job_id, metadata
    )


def renew_lease(
    scope: str,
    holder: str,
    ttl_seconds: Optional[float] = None,
) -> MaintenanceLease:
    """Renew a lease using the global manager."""
    return get_lease_manager().renew(scope, holder, ttl_seconds)


def release_lease(scope: str, holder: str) -> bool:
    """Release a lease using the global manager."""
    return get_lease_manager().release(scope, holder)


def recover_expired_leases(at: Optional[datetime] = None) -> list[MaintenanceLease]:
    """Recover expired leases using the global manager."""
    return get_lease_manager().recover_expired(at)


def check_lease_conflict(scope: str, holder: str) -> tuple[bool, Optional[MaintenanceLease]]:
    """Check if a lease would conflict."""
    lease = get_lease_manager().get(scope)
    if lease is None:
        return False, None
    if lease.holder == holder:
        return False, lease
    return True, lease


__all__ = [
    "MaintenanceLease",
    "LeaseConflictError",
    "LeaseManager",
    "get_lease_manager",
    "acquire_lease",
    "renew_lease",
    "release_lease",
    "recover_expired_leases",
    "check_lease_conflict",
]
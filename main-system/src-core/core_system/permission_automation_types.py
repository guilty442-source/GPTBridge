"""Types and dataclasses for permission automation — A6/A10/A11/A22.

This module contains all enums and dataclasses used by the permission
automation submodules.  No imports from other permission-automation
submodules (single-responsibility: types only).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional


class PermissionGrantState(Enum):
    """權限授予狀態。"""
    ACTIVE = "active"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    REVOKED = "revoked"
    SUSPENDED = "suspended"
    PENDING_RENEWAL = "pending_renewal"


class ComplianceSeverity(Enum):
    """合規違規嚴重程度。"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class PermissionGrant:
    """權限授予記錄。"""
    grant_id: str
    actor: str
    capability: str
    action: str
    target: str
    data_scope: Optional[str]
    issued_at: datetime
    expires_at: Optional[datetime] = None
    state: PermissionGrantState = PermissionGrantState.ACTIVE
    renewed_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoked_reason: Optional[str] = None
    auto_renew: bool = True
    renewal_count: int = 0
    last_checked: Optional[datetime] = None

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    def is_expiring_soon(self, days: int = 7) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(days=days)

    def time_until_expiry(self) -> Optional[timedelta]:
        if self.expires_at is None:
            return None
        return self.expires_at - datetime.now(timezone.utc)


@dataclass
class ComplianceViolation:
    """合規違規記錄。"""
    violation_id: str
    severity: ComplianceSeverity
    actor: str
    capability: str
    target: str
    description: str
    detected_at: datetime
    resolved: bool = False
    resolved_at: Optional[datetime] = None
    resolution_action: Optional[str] = None
    auto_resolvable: bool = False


@dataclass
class AuditSchedule:
    """審計排程。"""
    audit_id: str
    audit_type: str
    schedule: str  # cron-like or interval
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    enabled: bool = True
    last_result: Optional[dict] = None


__all__ = [
    "PermissionGrantState",
    "ComplianceSeverity",
    "PermissionGrant",
    "ComplianceViolation",
    "AuditSchedule",
]

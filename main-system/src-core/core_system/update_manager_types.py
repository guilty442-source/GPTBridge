"""Types and dataclasses for update_manager — A181/A182/A183.

Enums and dataclasses used by the update manager submodules.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class UpdateStatus(Enum):
    """Update lifecycle status."""
    IDLE = "idle"
    CHECKING = "checking"
    PREPARING = "preparing"
    RELOADING = "reloading"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class UpdateType(Enum):
    """Type of update."""
    HOT_RELOAD = "hot_reload"
    GENERATION_PREPARE = "generation_prepare"
    CERTIFIED_UPDATE = "certified_update"
    MANUAL = "manual"


@dataclass
class UpdateManifest:
    """Manifest tracking an update operation."""
    update_id: str
    update_type: UpdateType
    status: UpdateStatus
    started_at: str
    completed_at: Optional[str] = None
    modules: list[str] = field(default_factory=list)
    module_hashes: dict[str, str] = field(default_factory=dict)
    pre_reload_pointer: Optional[dict] = None
    post_reload_pointer: Optional[dict] = None
    error: Optional[str] = None
    health_checks: dict[str, bool] = field(default_factory=dict)
    rollback_reason: Optional[str] = None
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HealthCheckResult:
    """Result of a health check."""
    name: str
    passed: bool
    message: str
    timestamp: str
    duration_ms: int


__all__ = [
    "UpdateStatus",
    "UpdateType",
    "UpdateManifest",
    "HealthCheckResult",
]

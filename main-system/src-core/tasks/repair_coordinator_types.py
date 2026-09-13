"""Repair coordinator — types and constants.

Extracted from repair_coordinator.py: RepairLock dataclass,
version/stale constants, and the ISO timestamp helper.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Final

from core_system.versioning import component_version

REPAIR_COORDINATOR_VERSION: Final[str] = component_version("repair-coordinator")

# How long a repair lock is considered valid before it's treated as stale
# (the owner likely crashed).  This bounds the window for duplicate repair.
REPAIR_LOCK_STALE_SECONDS: Final[float] = 120.0


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RepairLock:
    """A held repair lock for a failure domain."""

    lock_id: str
    failure_code: str
    owner: str
    acquired_at: str
    expires_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_stale(self, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        try:
            expires = datetime.fromisoformat(self.expires_at).timestamp()
            return now > expires
        except (ValueError, OSError):
            return True

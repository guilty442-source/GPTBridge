"""Repair coordinator — A152/A154 governed repair path with owner coordination.

Per the amended Governance Codex (A152 supersedes A67, A154 supersedes
A72, E128 supersedes E52), the failure path must follow:

  maintenance-classifies-health-signal
    > decision-sovereign-decides-repair
    > permission-validation
    > system-runtime-or-system-programming-dispatch
    > governed-executor-repairs
    > independent-verification
    > information-layer-status-event-audit
    > ui-resynchronizes

And FORBID:duplicate-repair-owner — only one repair owner may act at a
time.  Both the boot_core connection watchdog and the frontend's
``app:restart-backend`` are potential repair owners; this coordinator
ensures they do not race or duplicate.

The coordinator provides:
  * A process-wide repair lock (thread-safe) so only one repair runs at a
    time for a given failure domain.
  * A governed repair entry point that records the failure in the learning
    store, consults learned recipes, and delegates to the central repair
    service — all under the decision-sovereign's repair decision
    authority (A152).
  * A status query so the frontend can check whether a repair is already
    in progress before attempting its own restart.

This module runs inside the backend (main.py --serve).  The boot_core
connection watchdog communicates with it via the shared SQLite learning
store and the repair state file, since boot_core and the backend are
separate processes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from core_system.auto_repair_chain import (
    AutoRepairOrchestrator,
    create_auto_repair_orchestrator,
)
from governance_rule.execution.authentication import GovernanceAuthenticationService

from .repair_coordinator_types import (
    REPAIR_COORDINATOR_VERSION,
    REPAIR_LOCK_STALE_SECONDS,
    RepairLock,
    _iso_now,
)
from .repair_coordinator_requests import RepairCoordinatorRequestsMixin
from .repair_coordinator_governed import RepairGovernedMixin


class RepairCoordinator(RepairCoordinatorRequestsMixin, RepairGovernedMixin):
    """Coordinates repair ownership to prevent duplicate repair owners.

    Thread-safe.  Lives in the backend process.  The boot_core connection
    watchdog communicates via the shared state file since it's a separate
    process.
    """

    VERSION = REPAIR_COORDINATOR_VERSION

    def __init__(self, project_root: Path, auth_service: GovernanceAuthenticationService | None = None) -> None:
        self.project_root = project_root.resolve()
        self._lock = threading.Lock()
        self._held_lock: RepairLock | None = None
        self._state_file = (
            self.project_root
            / "main-system"
            / "runtime"
            / "state"
            / "repair-coordination.json"
        )
        # A261: Auto-repair orchestrator for governance-compliant repair chain
        self._orchestrator: AutoRepairOrchestrator | None = None
        if auth_service is not None:
            self._orchestrator = create_auto_repair_orchestrator(project_root, auth_service)

    def _now_ts(self) -> float:
        return time.time()

    def _write_state(self) -> None:
        """Write the current lock state to disk for cross-process visibility."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": REPAIR_COORDINATOR_VERSION,
                "held_lock": self._held_lock.as_dict() if self._held_lock else None,
                "updated_at": _iso_now(),
            }
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self._state_file)
        except OSError:
            pass

    def _read_state(self) -> dict[str, Any] | None:
        """Read the lock state written by any process (boot_core or backend)."""
        if not self._state_file.is_file():
            return None
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def try_acquire(
        self,
        failure_code: str,
        owner: str,
        ttl_seconds: float = REPAIR_LOCK_STALE_SECONDS,
    ) -> bool:
        """Try to acquire the repair lock for a failure domain.

        Returns True if acquired (or if the existing lock is stale and was
        taken over).  Returns False if another owner already holds a valid
        lock — the caller must NOT proceed with repair in that case.
        """
        with self._lock:
            now = self._now_ts()
            # Check the on-disk state first (cross-process: boot_core watchdog).
            disk_state = self._read_state()
            if disk_state and disk_state.get("held_lock"):
                disk_lock = RepairLock(**disk_state["held_lock"])
                if not disk_lock.is_stale(now):
                    # Another process holds a valid lock.
                    if disk_lock.owner != owner or disk_lock.failure_code != failure_code:
                        return False
                    # Same owner + same failure — re-acquire (idempotent).
            # Check in-process lock.
            if self._held_lock is not None and not self._held_lock.is_stale(now):
                if self._held_lock.owner != owner or self._held_lock.failure_code != failure_code:
                    return False
            # Acquire.
            lock_id = uuid4().hex
            acquired = datetime.fromtimestamp(now, tz=timezone.utc)
            expires = datetime.fromtimestamp(now + ttl_seconds, tz=timezone.utc)
            self._held_lock = RepairLock(
                lock_id=lock_id,
                failure_code=failure_code,
                owner=owner,
                acquired_at=acquired.isoformat(),
                expires_at=expires.isoformat(),
            )
            self._write_state()
            return True

    def release(self, owner: str, failure_code: str) -> None:
        """Release the repair lock if held by the given owner."""
        with self._lock:
            if (
                self._held_lock is not None
                and self._held_lock.owner == owner
                and self._held_lock.failure_code == failure_code
            ):
                self._held_lock = None
                self._write_state()

    def is_repair_in_progress(self, failure_code: str | None = None) -> bool:
        """Check if any repair is in progress (optionally for a specific failure)."""
        with self._lock:
            now = self._now_ts()
            # Check in-process.
            if self._held_lock is not None and not self._held_lock.is_stale(now):
                if failure_code is None or self._held_lock.failure_code == failure_code:
                    return True
            # Check on-disk (cross-process).
            disk_state = self._read_state()
            if disk_state and disk_state.get("held_lock"):
                disk_lock = RepairLock(**disk_state["held_lock"])
                if not disk_lock.is_stale(now):
                    if failure_code is None or disk_lock.failure_code == failure_code:
                        return True
            return False

    def get_status(self) -> dict[str, Any]:
        """Return the current coordination status for observability."""
        with self._lock:
            now = self._now_ts()
            held = self._held_lock
            if held is not None and held.is_stale(now):
                held = None
            return {
                "version": REPAIR_COORDINATOR_VERSION,
                "repair_in_progress": held is not None,
                "held_lock": held.as_dict() if held else None,
                "updated_at": _iso_now(),
            }

    # ------------------------------------------------------------------
    # A154 governed repair request — signal-and-request-only for boot_core
    # ------------------------------------------------------------------

    def _requests_file(self) -> Path:
        """Information-layer state file for governed repair requests."""
        return (
            self.project_root
            / "main-system"
            / "runtime"
            / "state"
            / "repair-requests.json"
        )

    def _read_requests(self) -> list[dict[str, Any]]:
        path = self._requests_file()
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _write_requests(self, requests: list[dict[str, Any]]) -> None:
        path = self._requests_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(requests, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass


# Module-level singleton — initialized lazily by the backend on startup.
_coordinator: RepairCoordinator | None = None


def get_repair_coordinator(project_root: Path | None = None) -> RepairCoordinator | None:
    """Get the process-wide repair coordinator, or None if not initialized."""
    global _coordinator
    if _coordinator is None and project_root is not None:
        _coordinator = RepairCoordinator(project_root)
    return _coordinator


def init_repair_coordinator(project_root: Path) -> RepairCoordinator:
    """Initialize the process-wide repair coordinator."""
    global _coordinator
    _coordinator = RepairCoordinator(project_root)
    return _coordinator


__all__ = [
    "REPAIR_COORDINATOR_VERSION",
    "REPAIR_LOCK_STALE_SECONDS",
    "RepairCoordinator",
    "RepairLock",
    "get_repair_coordinator",
    "init_repair_coordinator",
]

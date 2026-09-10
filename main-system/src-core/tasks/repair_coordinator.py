"""Repair coordinator — A152/A154 governed repair path with owner coordination.

Per the amended Governance Codex (A152 supersedes A67, A154 supersedes
A72, E128 supersedes E52), the failure path must follow:

  maintenance-classifies-health-signal
    > system-decision-sovereign-decides-repair
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
    service — all under the system-decision-sovereign's repair decision
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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

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


class RepairCoordinator:
    """Coordinates repair ownership to prevent duplicate repair owners.

    Thread-safe.  Lives in the backend process.  The boot_core connection
    watchdog communicates via the shared state file since it's a separate
    process.
    """

    VERSION = REPAIR_COORDINATOR_VERSION

    def __init__(self, project_root: Path) -> None:
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

    def request_governed_repair(
        self,
        failure_code: str,
        owner: str,
        *,
        decision_proof: dict[str, Any],
        repair_executor: Any | None = None,
        signal_only: bool = False,
    ) -> dict[str, Any]:
        """A154 governed repair entry — records decision proof before mutation.

        Per A154 (supersedes A72): ``DECISION-PROOF:required-before-repair-
        mutation`` and ``BOOT-CORE+WATCHDOG+UI+MODULE:signal-and-request-only``.

        * ``decision_proof`` — the governance authorization that permits
          this repair (e.g. governance bootstrap attestation for crash
          repair, or a system-decision-sovereign decision token for live
          repair).
        * ``repair_executor`` — callable that performs the actual repair
          mutation.  Required for crash repair (backend is dead, no
          system-decision-sovereign is available).  When ``None`` or
          ``signal_only=True``, only a signal is written to the
          information layer for the system-decision-sovereign to pick up.
        * Returns a report dict with the repair outcome or signal record.

        This method acquires the coordination lock (FORBID:duplicate-repair-owner),
        records the request + decision proof to the information layer, and
        either executes the repair (crash case) or leaves a pending signal
        (backend-alive case) for the system-decision-sovereign repair
        decision chain.
        """
        report: dict[str, Any] = {
            "governed": True,
            "failure_code": failure_code,
            "owner": owner,
            "signal_only": signal_only or repair_executor is None,
            "ok": False,
        }

        # FORBID:duplicate-repair-owner — acquire lock first.
        if not self.try_acquire(failure_code=failure_code, owner=owner):
            report["reason"] = "duplicate-repair-owner; another owner holds the lock"
            report["decision_proof"] = decision_proof
            return report

        request_id = uuid4().hex
        request_record: dict[str, Any] = {
            "request_id": request_id,
            "failure_code": failure_code,
            "owner": owner,
            "decision_proof": decision_proof,
            "requested_at": _iso_now(),
            "status": "pending",
            "signal_only": signal_only or repair_executor is None,
        }

        if signal_only or repair_executor is None:
            # Signal-only: write to information layer, maintenance sovereign
            # will pick up and make the repair decision.
            requests = self._read_requests()
            requests.append(request_record)
            self._write_requests(requests)
            report["request_id"] = request_id
            report["ok"] = True
            report["reason"] = "signal written to information layer; awaiting system-decision-sovereign decision"
            self.release(owner=owner, failure_code=failure_code)
            return report

        # Crash repair: backend is dead, system-decision-sovereign unavailable.
        # The governance bootstrap attestation IS the decision proof.
        # Execute the repair mutation under the coordination lock.
        request_record["status"] = "executing"
        requests = self._read_requests()
        requests.append(request_record)
        self._write_requests(requests)

        try:
            result = repair_executor()  # type: ignore[misc]
            report["ok"] = bool(result.get("ok")) if isinstance(result, dict) else True
            report["result"] = result
            request_record["status"] = "completed" if report["ok"] else "failed"
            request_record["completed_at"] = _iso_now()
        except Exception as error:
            report["ok"] = False
            report["error"] = f"{type(error).__name__}: {error}"
            request_record["status"] = "failed"
            request_record["error"] = report["error"]
            request_record["completed_at"] = _iso_now()

        # Update the request record in the information layer.
        requests = self._read_requests()
        for i, req in enumerate(requests):
            if req.get("request_id") == request_id:
                requests[i] = request_record
                break
        self._write_requests(requests)

        self.release(owner=owner, failure_code=failure_code)
        return report

    def pending_requests(self) -> list[dict[str, Any]]:
        """Return pending repair requests for the system-decision-sovereign to process."""
        return [
            req for req in self._read_requests()
            if req.get("status") == "pending"
        ]

    def acknowledge_request(
        self,
        request_id: str,
        *,
        system_decision: str,
        ok: bool,
    ) -> None:
        """Record the system-decision-sovereign's decision on a repair request."""
        requests = self._read_requests()
        for req in requests:
            if req.get("request_id") == request_id:
                req["status"] = system_decision
                req["sovereign_decided_at"] = _iso_now()
                req["sovereign_decision_ok"] = ok
                break
        self._write_requests(requests)


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

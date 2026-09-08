"""Data Sub-Sovereign (system) — owns ALL data-body-related concerns, in-process.

Per the Governance Codex (A31 / E18 / P14, absorbed under the System Sovereign),
the Data Sub-Sovereign is responsible for every data-BODY concern: structured data,
semantic index, and version history access specifications, consistency, integrity
checks, and data directory.  It is the sole owner of data-body matters; no other
sovereign or module may take them over (E20 boundary).

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates existing
data executors and DELEGATES the actual data operations to governed
executors; it never holds an execution power itself.
"""

from __future__ import annotations

from typing import Any

from .codex_decision import decision_basis
from .sovereign_utils import _iso_now
from core.health import check_core_health

DATA_SUB_SOVEREIGN_ROLE = "system-data-sub-sovereign"

SYSTEM_DATA_AUTHORITY = "data"


class DataSubSovereign:
    """In-process sub-sovereign (under system) responsible for ALL data-body functions.

    Responsibilities (any data-body-related function):
      - structured data access specifications
      - semantic index management
      - version history management
      - consistency and integrity checks
      - data directory maintenance
    """

    ROLE = DATA_SUB_SOVEREIGN_ROLE

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._integrity_checker: Any | None = None
        self._task_queue: Any | None = None
        self._data_health_checker: Any = check_core_health

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        integrity_checker: Any = None,
        task_queue: Any = None,
        data_health_checker: Any = None,
    ) -> dict[str, Any]:
        """Start the Data Sub-Sovereign.

        The sub-sovereign captures the data-body governed executors from the app
        (decision-only supervision; it never executes the heavy data work
        in-process):
          ``integrity_checker`` — callable returning a bool/status (defaults to
              ``app.governance.runtime_integrity_ready``) for consistency and
              integrity checks.
          ``task_queue`` — the TaskQueue for data consistency / recovery.
          ``data_health_checker`` — callable returning a health report dict
              (default ``core.health.check_core_health``).
        """

        governance = getattr(self.app, "governance", None)
        self._integrity_checker = integrity_checker
        if self._integrity_checker is None and governance is not None:
            self._integrity_checker = getattr(governance, "runtime_integrity_ready", None)
        self._task_queue = task_queue
        if self._task_queue is None:
            self._task_queue = getattr(self.app, "task_queue", None)
        if data_health_checker is not None:
            self._data_health_checker = data_health_checker
        self._started_at = _iso_now()
        self._started = True

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "decision": decision_basis(SYSTEM_DATA_AUTHORITY),
        }

    async def stop(self) -> None:
        self._integrity_checker = None
        self._task_queue = None
        self._started = False
        self._stopped_at = _iso_now()

    # ------------------------------------------------------------------
    # Data authority surface
    # ------------------------------------------------------------------

    def data_status(self) -> dict[str, Any]:
        """Snapshot the data-BODY state."""

        decision = decision_basis(SYSTEM_DATA_AUTHORITY)
        return {
            "role": self.ROLE,
            "scope": "all-data-body-functions",
            "structured_data": self._structured_data_status(),
            "semantic_index": self._semantic_index_status(),
            "version_history": self._version_history_status(),
            "consistency_integrity": self._consistency_integrity_status(),
            "data_directory": self._data_directory_status(),
            "decision": decision,
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "all-data-body-functions",
            "started": self._started,
            "structured_data": self._structured_data_status(),
            "semantic_index": self._semantic_index_status(),
            "version_history": self._version_history_status(),
            "consistency_integrity": self._consistency_integrity_status(),
            "data_directory": self._data_directory_status(),
            "decision": decision_basis(SYSTEM_DATA_AUTHORITY),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "name": "data",
            "role": self.ROLE,
            "scope": "all-data-body-functions",
            "state": "running" if self._started else "stopped",
            "delegation": "governed-executor-only",
            "integrity_ready": self._integrity_ready(),
            "data_directory_report": self._data_directory_report(),
            "decision": decision_basis(SYSTEM_DATA_AUTHORITY),
        }

    # ------------------------------------------------------------------
    # Data-BODY responsibility status surfaces
    # ------------------------------------------------------------------

    def _integrity_ready(self) -> bool | None:
        checker = self._integrity_checker
        if not callable(checker):
            return None
        try:
            return bool(checker())
        except Exception:
            return None

    def _structured_data_status(self) -> dict[str, Any]:
        return {
            "duty": "structured-data",
            "enabled": bool(self._task_queue is not None),
            "delegation": "governed-executor-only",
        }

    def _semantic_index_status(self) -> dict[str, Any]:
        return {
            "duty": "semantic-index",
            "enabled": False,
            "coordinator": "rag-bridge/vector-store (governed executor)",
            "delegation": "governed-executor-only",
        }

    def _version_history_status(self) -> dict[str, Any]:
        version = getattr(self.app, "version", None)
        return {
            "duty": "version-history",
            "version": version,
            "delegation": "governed-executor-only",
        }

    def _consistency_integrity_status(self) -> dict[str, Any]:
        integrity_ready = self._integrity_ready()
        recovery: list[dict[str, Any]] = []
        if self._task_queue is not None and hasattr(self._task_queue, "pending_recovery"):
            try:
                recovery = self._task_queue.pending_recovery() or []
            except Exception:
                recovery = []
        maintenance = getattr(self.app, "maintenance_sovereign", None)
        repair_service = getattr(maintenance, "_repair_service", None) if maintenance is not None else None
        return {
            "duty": "consistency-integrity",
            "integrity_ready": integrity_ready,
            "pending_recovery": recovery,
            "repair_service": repair_service is not None,
            "decision": decision_basis(SYSTEM_DATA_AUTHORITY)["edicts"],
        }

    def _data_directory_status(self) -> dict[str, Any]:
        return {
            "duty": "data-directory",
            "cleaner_enabled": self._cleaner_status(),
            "health_report": self._data_directory_report(),
            "delegation": "governed-executor-only",
        }

    def _cleaner_status(self) -> bool:
        maintenance = getattr(self.app, "maintenance_sovereign", None)
        if maintenance is None:
            return False
        status = getattr(maintenance, "_daily_cleaner_status", None)
        if not callable(status):
            return False
        try:
            report = status()
        except Exception:
            return False
        return bool(report and report.get("enabled"))

    def _data_directory_report(self) -> dict[str, Any]:
        checker = self._data_health_checker
        if not callable(checker):
            return {}
        try:
            return checker(getattr(self.app, "project_root", None))
        except Exception:
            return {"error": "data-health-checker-unavailable"}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

__all__ = [
    "SYSTEM_DATA_AUTHORITY",
    "DATA_SOVEREIGN_ROLE",
    "DataSubSovereign",
]

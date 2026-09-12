"""Data Governance Sub-Sovereign — 資料治理子主權（子屬決策主宰，無決策、無執行）。

法典依據:
- sovereign_id: data-governance-sub-sovereign (position 39)
- area: data-integrity
- rank: child-of-decision-sovereign-no-decision-no-execution
- basis: A304/A323 (retires data-sovereign / data-sub-sovereign)

The implementation was merged from the retired
``core_system.data_sub_sovereign.DataSubSovereign`` so the active path keeps
its data-integrity supervision behavior while operating under the codex
``data-governance-sub-sovereign`` identity.

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates existing
data executors and DELEGATES the actual data operations to governed
executors; it never holds an execution power itself.
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase
from core_system.codex_decision import decision_basis
from core_system.sovereign_utils import _iso_now
from core.health import check_core_health
from governance_rule.codex import GOVERNANCE_CODEX


_DATA_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "data-integrity"),
    None,
)
if _DATA_SOVEREIGN is None:
    raise RuntimeError("data governance sub-sovereign not found in Governance Codex")

SYSTEM_DATA_AUTHORITY = "data"


class DataGovernanceSubSovereign(SubSovereignBase):
    """In-process sub-sovereign responsible for ALL data-integrity functions.

    Responsibilities (codex sovereign definition — A304/A323):
      - govern-data-integrity (consistency and integrity checks)
      - decide-data-lifecycle (structured data / semantic index / version history)
      - decide-data-reconciliation (data directory recovery)
    """

    sovereign_id = "data-governance-sub-sovereign"
    parent_sovereign_id = "decision-sovereign"

    ROLE = sovereign_id

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._integrity_checker: Any | None = None
        self._task_queue: Any | None = None
        self._data_health_checker: Any = check_core_health
        self._data_specs: dict[str, dict[str, Any]] = {}

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
        """Start the Data Governance Sub-Sovereign.

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
            "scope": "data-integrity",
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
            "scope": "data-integrity",
            "parent": self.parent_sovereign_id,
            "started": self._started,
            "structured_data": self._structured_data_status(),
            "semantic_index": self._semantic_index_status(),
            "version_history": self._version_history_status(),
            "consistency_integrity": self._consistency_integrity_status(),
            "data_directory": self._data_directory_status(),
            "data_specs": list(self._data_specs.keys()),
            "decision": decision_basis(SYSTEM_DATA_AUTHORITY),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "name": "data",
            "role": self.ROLE,
            "scope": "data-integrity",
            "state": "running" if self._started else "stopped",
            "delegation": "governed-executor-only",
            "integrity_ready": self._integrity_ready(),
            "data_directory_report": self._data_directory_report(),
            "decision": decision_basis(SYSTEM_DATA_AUTHORITY),
        }

    # ------------------------------------------------------------------
    # Data-BODY responsibility status surfaces
    # ------------------------------------------------------------------

    def register_data_spec(self, spec_id: str, spec: dict[str, Any]) -> None:
        self._data_specs[spec_id] = {
            "spec": spec,
            "registered_at": self._iso_now(),
            "status": "active",
        }

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
            "duty": "decide-data-lifecycle",
            "enabled": bool(self._task_queue is not None),
            "delegation": "governed-executor-only",
        }

    def _semantic_index_status(self) -> dict[str, Any]:
        return {
            "duty": "decide-data-lifecycle",
            "enabled": False,
            "coordinator": "qdrant (canonical); local-vector-store as bounded degraded fallback (governed executor)",
            "delegation": "governed-executor-only",
        }

    def _version_history_status(self) -> dict[str, Any]:
        version = getattr(self.app, "version", None)
        return {
            "duty": "decide-data-lifecycle",
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
        executors = self._maintenance_executor_status()
        return {
            "duty": "govern-data-integrity",
            "integrity_ready": integrity_ready,
            "pending_recovery": recovery,
            "repair_delegated": bool(executors.get("central_repair")),
            "executor_owner": executors.get("owner", self.ROLE),
            "decision": decision_basis(SYSTEM_DATA_AUTHORITY)["edicts"],
        }

    def _data_directory_status(self) -> dict[str, Any]:
        executors = self._maintenance_executor_status()
        return {
            "duty": "decide-data-reconciliation",
            "cleanup_delegated": bool(executors.get("daily_global_cleaner")),
            "executor_owner": executors.get("owner", self.ROLE),
            "health_report": self._data_directory_report(),
            "delegation": "governed-executor-only",
        }

    def _maintenance_executor_status(self) -> dict[str, Any]:
        maintenance = getattr(self.app, "maintenance_sovereign", None)
        if maintenance is None:
            return {}
        status = getattr(maintenance, "executor_ownership_status", None)
        if not callable(status):
            return {}
        try:
            return status()
        except Exception:
            return {}

    def _data_directory_report(self) -> dict[str, Any]:
        checker = self._data_health_checker
        if not callable(checker):
            return {}
        try:
            return checker(getattr(self.app, "project_root", None))
        except Exception:
            return {"error": "data-health-checker-unavailable"}


__all__ = ["DataGovernanceSubSovereign", "SYSTEM_DATA_AUTHORITY"]

"""Health Maintenance Test Sub-Sovereign — 健康維護測試子主權（子屬決策主宰，無決策、無執行）。

法典依據:
- sovereign_id: health-maintenance-test-sub-sovereign (position 38)
- area: maintenance
- rank: child-of-decision-sovereign-no-decision-no-execution
- basis: A302/A323 (retires maintenance-sovereign / maintenance-sub-sovereign)

The implementation was merged from the retired
``core_system.maintenance_sovereign.MaintenanceSovereign`` so the active
path keeps its system-health monitoring / preservation / classification
behavior while operating under the codex
``health-maintenance-test-sub-sovereign`` identity.

Its duties remain health-focused:
  * monitor-system-health  — system health monitoring (incl. data-integrity
                              health presentation; check execution is owned
                              by the data governance sub-sovereign)
  * preserve-system-health — preserve system health (backup coordination,
                              health restoration acceptance)
  * maintain-system         — maintain system health (delegated cleanup,
                              update-boundary supervision)

Authority boundaries (A152/A154/E127/E128): the scope is **health-only**
(A154).  Repair decisions are owned by the decision-sovereign (A152);
runtime actions (hot-reload) by system-runtime (E127); code changes by the
release-update synchronization chain (E127).  The sub-sovereign classifies
health signals and delegates; it never owns the repair decision, performs
code changes, or handles permissions.

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates existing
in-process services (injected as references) and delegates heavy execution
to governed executors; it never runs that heavy work in the mother process.

Implementation note: the sub-sovereign is composed from focused mixins in
``core_system`` to keep each source file under 500 lines:

  * ``maintenance_learning``    — persistent learning state (survives restarts)
  * ``maintenance_lifecycle``   — start / stop lifecycle
  * ``maintenance_status``      — status reporting + health monitoring
  * ``maintenance_update``      — update / repair / fault / backup health surfaces
  * ``maintenance_capability``  — capability / installation detection
  * ``maintenance_repair_chain`` — A152/A154 health-classification chain
"""

from __future__ import annotations

import asyncio
from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX

from ._base import SubSovereignBase
from core_system.maintenance_learning import MaintenanceLearningMixin
from core_system.maintenance_lifecycle import MaintenanceLifecycleMixin
from core_system.maintenance_status import MaintenanceStatusMixin
from core_system.maintenance_update import MaintenanceUpdateMixin
from core_system.maintenance_capability import MaintenanceCapabilityMixin
from core_system.maintenance_repair_chain import MaintenanceRepairChainMixin
from core.health import check_core_health


_MAINTENANCE_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "maintenance"),
    None,
)
if _MAINTENANCE_SOVEREIGN is None:
    raise RuntimeError("health maintenance test sub-sovereign not found in Governance Codex")

MAINTENANCE_RESPONSIBILITIES = _MAINTENANCE_SOVEREIGN.duties


class HealthMaintenanceTestSubSovereign(
    MaintenanceLifecycleMixin,
    MaintenanceStatusMixin,
    MaintenanceUpdateMixin,
    MaintenanceCapabilityMixin,
    MaintenanceRepairChainMixin,
    SubSovereignBase,
):
    """In-process sub-sovereign responsible for ALL system-health functions.

    Responsibilities (health-only scope, A302/A323/A154):
      - monitor-system-health (incl. data-integrity health presentation)
      - preserve-system-health (backup coordination, health restoration)
      - maintain-system (delegated cleanup, update-boundary supervision)

    Repair decisions are delegated to the decision-sovereign
    (A152); runtime actions to system-runtime (E127); code changes to the
    release-update synchronization chain (E127).  The sub-sovereign
    classifies health signals and monitors system health; it never owns
    non-health decisions (A152: FORBID:maintenance-owning-non-health-decisions).
    """

    sovereign_id = "health-maintenance-test-sub-sovereign"
    parent_sovereign_id = "decision-sovereign"

    ROLE = sovereign_id

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._daily_cleaner: Any | None = None
        self._hot_update: Any | None = None
        self._repair_service: Any | None = None
        self._health_checker: Any = check_core_health
        self._capability_task: asyncio.Task[Any] | None = None
        self._capability_interval_seconds = 3600.0
        self._capability_report: dict[str, Any] | None = None
        # Persistent learning state — survives auto-repair restarts because
        # it is backed by the SQLite repair-learning store.  The sub-sovereign
        # loads the last analysis on start so its knowledge is not reset by
        # a backend crash/restart cycle.
        self._learning_store: Any | None = None
        self._learner: Any | None = None
        self._learning_analysis: dict[str, Any] | None = None
        # A152/A154 health-classification chain task.
        self._repair_decision_task: asyncio.Task[Any] | None = None
        # Health check registry.
        self._health_checks: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Health check registry
    # ------------------------------------------------------------------

    def register_health_check(self, check_id: str, spec: dict[str, Any]) -> None:
        self._health_checks[check_id] = {
            "spec": spec,
            "registered_at": self._iso_now(),
            "status": "registered",
        }

    def record_health_result(self, check_id: str, result: dict[str, Any]) -> None:
        if check_id in self._health_checks:
            self._health_checks[check_id].update({
                "last_result": result,
                "checked_at": self._iso_now(),
                "status": "completed",
            })


__all__ = ["HealthMaintenanceTestSubSovereign"]

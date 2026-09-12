"""Maintenance Sovereign — owns ALL system-health functionality.

The Maintenance Sovereign is the system-health owner per the amended
Governance Codex (A125/E102).  Its duties are health-focused:

  * monitor-system-health  — system health monitoring (incl. data-integrity
                              health presentation; check execution is owned
                              by the data sovereign, A33/E20)
  * preserve-system-health — preserve system health (backup coordination,
                              health restoration acceptance)
  * maintain-system         — maintain system health (delegated cleanup,
                              update-boundary supervision)

Its powers (codex sovereign definition):
  * declare-system-health-state
  * dispatch-governed-health-maintenance
  * accept-health-restoration

Its prohibitions:
  * direct-ungoverned-execution
  * cross-sovereign-duty-takeover
  * permission-self-authorization

Authority boundaries (A152/A154/E127/E128): the maintenance sovereign's
scope is **health-only** (A154).  Repair decisions are owned by the
decision-sovereign (A152); runtime actions (hot-reload) by
system-runtime (E127); code changes by system-programming (E127);
learning by the learning-system sovereign (E127).  The maintenance
sovereign classifies health signals and delegates; it never owns the
repair decision, performs code changes, or handles permissions.

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates existing
in-process services (injected as references) and delegates heavy execution
to governed executors; it never runs that heavy work in the mother process.

Implementation note: the sovereign is composed from focused mixins to keep
each source file under 500 lines:

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

from .maintenance_learning import MaintenanceLearningMixin
from .maintenance_lifecycle import MaintenanceLifecycleMixin
from .maintenance_status import MaintenanceStatusMixin
from .maintenance_update import MaintenanceUpdateMixin
from .maintenance_capability import MaintenanceCapabilityMixin
from .maintenance_repair_chain import MaintenanceRepairChainMixin
from core.health import check_core_health


_MAINTENANCE_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "maintenance"),
    None,
)
if _MAINTENANCE_SOVEREIGN is None:
    raise RuntimeError("maintenance sovereign not found in Governance Codex")

MAINTENANCE_RESPONSIBILITIES = _MAINTENANCE_SOVEREIGN.duties


class MaintenanceSovereign(
    MaintenanceLifecycleMixin,
    MaintenanceStatusMixin,
    MaintenanceUpdateMixin,
    MaintenanceCapabilityMixin,
    MaintenanceRepairChainMixin,
):
    """In-process sovereign responsible for ALL system-health functions.

    Responsibilities (A125/E102 — health-only scope, A154):
      - monitor-system-health (incl. data-integrity health presentation)
      - preserve-system-health (backup coordination, health restoration)
      - maintain-system (delegated cleanup, update-boundary supervision)

    Repair decisions are delegated to the decision-sovereign
    (A152); runtime actions to system-runtime (E127); code changes to
    system-programming (E127).  The maintenance sovereign classifies
    health signals and monitors system health; it never owns non-health
    decisions (A152: FORBID:maintenance-owning-non-health-decisions).
    """

    ROLE = _MAINTENANCE_SOVEREIGN.id

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
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
        # it is backed by the SQLite repair-learning store.  The sovereign
        # loads the last analysis on start so its knowledge is not reset by
        # a backend crash/restart cycle.
        self._learning_store: Any | None = None
        self._learner: Any | None = None
        self._learning_analysis: dict[str, Any] | None = None
        # A152/A154 health-classification chain task.
        self._repair_decision_task: asyncio.Task[Any] | None = None


__all__ = ["MAINTENANCE_RESPONSIBILITIES", "MaintenanceSovereign"]

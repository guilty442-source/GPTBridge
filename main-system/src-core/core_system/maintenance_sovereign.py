"""Maintenance Sovereign — owns ALL system-maintenance-related functionality.

The Maintenance Sovereign is responsible for every system-maintenance concern
per the Governance Codex:

  * update                — system / module updates
  * system health         — system health monitoring (including data integrity)
  * automatic repair      — system automatic repair
  * fault determination   — system fault / failure determination
  * backup                — backup coordination

It is the sole owner of system-maintenance matters; no module or other
authority may take over maintenance concerns.  It is LOCAL CODE (same process
as GPTBridgeApp) that coordinates existing in-process services (injected as
references) and delegates heavy execution to governed executors; it never runs
that heavy work in the mother process.

Implementation note: the sovereign is composed from focused mixins to keep
each source file under 500 lines:

  * ``maintenance_learning``    — persistent learning state (survives restarts)
  * ``maintenance_lifecycle``   — start / stop lifecycle
  * ``maintenance_status``      — status reporting + health monitoring
  * ``maintenance_update``      — update / hot-reload / repair / fault / backup
  * ``maintenance_capability``  — capability / installation detection
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
    """In-process sovereign responsible for ALL system-maintenance functions.

    Responsibilities (any system-maintenance-related function):
      - update
      - system health monitoring (including data integrity)
      - automatic repair
      - fault determination
      - backup
      - (plus periodic/resource maintenance delegated to governed executors)
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
        # A67/A72 repair decision chain task.
        self._repair_decision_task: asyncio.Task[Any] | None = None


__all__ = ["MAINTENANCE_RESPONSIBILITIES", "MaintenanceSovereign"]

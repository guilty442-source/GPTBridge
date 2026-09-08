"""Runtime Sub-Sovereign (system) — keeps the platform running, in-process.

Per the Governance Codex (A28 / E7, absorbed under the System Sovereign), the
Runtime Sub-Sovereign owns the concerns required for the mother process to
stay alive and serve requests:

  * IPC server health / readiness contract
  * command surface (runtime bootstrap)
  * runtime status reporting
  * governance runtime integrity

Idle memory maintenance is owned by the Resource Sub-Sovereign.

It is LOCAL CODE (same process as GPTBridgeApp).  It does not import the heavy
AI/model stack; it coordinates existing in-process services that are injected
as references.  All supervision is decision-level: actual execution stays with
the governed services / executors.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .codex_decision import decision_basis
from .sovereign_utils import _iso_now, _suppress
from .native import (
    monotonic_seconds,
    native_available,
    resource_status,
)
from governance_rule.execution.tool_runtime.sub_sovereign import (
    SYSTEM_RUNTIME_AUTHORITY,
    SYSTEM_RUNTIME_ROLE,
)


class RuntimeSubSovereign:
    """In-process sub-sovereign (under system) responsible for platform liveness and serving.

    Responsibilities:
      - Report readiness of the runtime command surface
      - Expose live runtime status (scope, phases, versions)
      - Track governance runtime integrity readiness

    Idle memory maintenance is owned by the Resource Sub-Sovereign.
    """

    ROLE = SYSTEM_RUNTIME_ROLE

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._stopped_at: str | None = None
        self._started_at: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Start the Runtime Sub-Sovereign and its in-process service loops.

        Update ownership is intentionally excluded. The Maintenance Sovereign
        is the sole update-management owner.
        """

        self._started_at = _iso_now()
        self._started = True
        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "memory_maintenance": {"enabled": False},
        }

    async def stop(self) -> None:
        self._started = False
        self._stopped_at = _iso_now()

    # ------------------------------------------------------------------
    # Runtime supervision helpers
    # ------------------------------------------------------------------

    def mark_activity(self) -> None:
        """Runtime activity marker; idle memory maintenance is owned by Resource."""
        return

    @property
    def maintenance_ready(self) -> bool:
        return bool(getattr(self.app, "maintenance_ready", False))

    @property
    def serving(self) -> bool:
        return (
            getattr(self.app, "command_router", None) is not None
            and self._started
        )

    # ------------------------------------------------------------------
    # Runtime status
    # ------------------------------------------------------------------

    def live_status(self) -> dict[str, Any]:
        governance_integrity: bool | None = getattr(
            self.app, "governance_integrity_ready", None
        )
        return {
            "role": self.ROLE,
            "started": self._started,
            "serving": self.serving,
            "phase": getattr(self.app, "startup_phase", "unknown"),
            "maintenance_ready": self.maintenance_ready,
            "governance_integrity": governance_integrity,
            "runtime_scope": getattr(
                getattr(self.app, "command_router", None), "scope", "starting"
            ),
            "memory_maintenance": {"enabled": False},
            "native": resource_status(),
            "decision": decision_basis(SYSTEM_RUNTIME_AUTHORITY),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "state": "serving" if self.serving else ("starting" if self._started else "stopped"),
            "delegation": "governed-executor-only",
            "native_kernel": native_available(),
            "decision": decision_basis(SYSTEM_RUNTIME_AUTHORITY),
        }

__all__ = ["RuntimeSubSovereign"]

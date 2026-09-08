"""Runtime Sub-Sovereign (system) — keeps the platform running, in-process.

Per the Governance Codex (A28 / E7, absorbed under the System Sovereign), the
Runtime Sub-Sovereign owns the concerns required for the mother process to
stay alive and serve requests:

  * IPC server health / readiness contract
  * command surface (runtime bootstrap)
  * runtime status reporting
  * idle memory maintenance (runtime resource health)
  * governance runtime integrity

It is LOCAL CODE (same process as GPTBridgeApp).  It does not import the heavy
AI/model stack; it coordinates existing in-process services that are injected
as references.  All supervision is decision-level: actual execution stays with
the governed services / executors.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from .codex_decision import decision_basis
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
      - Own the idle memory maintenance loop (runtime resource health)
      - Track governance runtime integrity readiness
    """

    ROLE = SYSTEM_RUNTIME_ROLE

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._stopped_at: str | None = None
        self._started_at: str | None = None
        self._memory_task: asyncio.Task[Any] | None = None
        self._memory_maintainer: Any | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, *, memory_maintainer: Any = None) -> dict[str, Any]:
        """Start the Runtime Sub-Sovereign and its in-process service loops.

        Update ownership is intentionally excluded. The Maintenance Sovereign
        is the sole update-management owner.
        """

        self._started_at = self._iso_now()
        self._started = True
        self._memory_maintainer = memory_maintainer
        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "memory_maintenance": (
                self._memory_maintainer.status()
                if self._memory_maintainer is not None
                else {"enabled": False}
            ),
        }

    async def stop(self) -> None:
        if self._memory_task is not None:
            self._memory_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._memory_task
            self._memory_task = None
        self._memory_maintainer = None
        self._started = False
        self._stopped_at = self._iso_now()

    # ------------------------------------------------------------------
    # Runtime supervision helpers
    # ------------------------------------------------------------------

    def mark_activity(self) -> None:
        maintainer = self._memory_maintainer
        if maintainer is not None and hasattr(maintainer, "mark_activity"):
            try:
                maintainer.mark_activity()
            except Exception:
                pass

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
            "memory_maintenance": (
                self._memory_maintainer.status()
                if self._memory_maintainer is not None
                else {"enabled": False}
            ),
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

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()


def _suppress(*exceptions: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(*exceptions)


__all__ = ["RuntimeSubSovereign"]

"""Runtime Sovereign — keeps the platform running, in-process.

The Runtime Sovereign owns the concerns required for the mother process to
stay alive and serve requests:

  * IPC server health / readiness contract
  * command surface (runtime bootstrap)
  * runtime status reporting
  * idle memory maintenance (runtime resource health)
  * version-gated hot update boundary
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
from .versioning import application_version


class RuntimeSovereign:
    """In-process sovereign responsible for platform liveness and serving.

    Responsibilities:
      - Report readiness of the runtime command surface
      - Expose live runtime status (scope, phases, versions)
      - Own the idle memory maintenance loop (runtime resource health)
      - Honor the hot-update boundary (frozen until governance authorizes)
      - Track governance runtime integrity readiness
    """

    ROLE = "runtime-sovereign"

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._stopped_at: str | None = None
        self._started_at: str | None = None
        self._memory_task: asyncio.Task[Any] | None = None
        self._memory_maintainer: Any | None = None
        self._hot_update: Any | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, *, memory_maintainer: Any = None) -> dict[str, Any]:
        """Start the Runtime Sovereign and its in-process service loops.

        Hot update is treated as a frozen, governance-gated boundary: the
        HotUpdateService is captured but NOT started here, because its
        ``start()`` raises PermissionError until governance authorizes an
        explicit versioned update.  The sovereign only supervises and reports
        the boundary; actual application is delegated to the governed service.
        """

        self._started_at = self._iso_now()
        self._started = True
        self._memory_maintainer = memory_maintainer
        self._hot_update = getattr(self.app, "hot_update_service", None)
        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "memory_maintenance": (
                self._memory_maintainer.status()
                if self._memory_maintainer is not None
                else {"enabled": False}
            ),
            "hot_update": self.hot_update_status(),
        }

    async def stop(self) -> None:
        if self._memory_task is not None:
            self._memory_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._memory_task
            self._memory_task = None
        self._memory_maintainer = None
        self._hot_update = None
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
    # Hot-update boundary (governance-gated, decision-level)
    # ------------------------------------------------------------------

    def hot_update_status(self) -> dict[str, Any]:
        """Snapshot the version-gated hot-update boundary.

        The hot-update boundary is FROZEN until governance authorizes an
        explicit versioned update command.  The sovereign owns the decision
        surface (what ``can_apply`` reports and the version context) without
        importing the heavy update executor in-process.
        """

        hot_update = self._hot_update
        app_version = application_version(self.app.project_root)

        if hot_update is None:
            return {
                "boundary": "frozen",
                "available": False,
                "version": app_version,
                "reason": "no-hot-update-service",
                "decision": decision_basis("hot-update"),
            }

        # The bound service stays start()-closed (PermissionError) until a
        # governance-authorized versioned update flips it.  The sovereign
        # reports that frozen state rather than forcing start(), which would
        # raise during startup.
        return {
            "boundary": "frozen",
            "available": True,
            "version": app_version,
            "interval_seconds": getattr(hot_update, "interval_seconds", 1.0),
            "gate": "governance",
            "permission": self._hot_update_permission(),
            "decision": decision_basis("hot-update"),
        }

    def _hot_update_permission(self) -> dict[str, Any]:
        """Decision-level authorization signal from the permission master-entry (best-effort)."""

        permission = self._permission_master_entry()
        if permission is None:
            return {"allowed": False, "reason": "no-permission-master-entry"}
        authorize = getattr(permission, "authorize_hot_update", None)
        if not callable(authorize):
            return {"allowed": False, "reason": "no-authorize"}
        try:
            authorize("xingcheng", "apply", "0.0.0", "tool-code")
        except PermissionError:
            return {"allowed": False, "reason": "not-authorized"}
        except Exception:
            return {"allowed": False, "reason": "error"}
        return {"allowed": True, "reason": "authorized"}

    def _permission_master_entry(self) -> Any:
        app = self.app
        sovereign_service = getattr(app, "system_sovereign_service", None)
        if sovereign_service is None:
            return None
        return getattr(sovereign_service, "permission_sovereign", None)

    # ------------------------------------------------------------------
    # Runtime status
    # ------------------------------------------------------------------

    def live_status(self) -> dict[str, Any]:
        governance = getattr(self.app, "governance", None)
        governance_integrity = bool(
            governance is not None
            and hasattr(governance, "runtime_integrity_ready")
            and governance.runtime_integrity_ready()
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
            "hot_update": self.hot_update_status(),
            "native": resource_status(),
            "decision": decision_basis("runtime"),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "state": "serving" if self.serving else ("starting" if self._started else "stopped"),
            "delegation": "governed-executor-only",
            "native_kernel": native_available(),
            "hot_update": self.hot_update_status(),
            "decision": decision_basis("runtime"),
        }

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()


def _suppress(*exceptions: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(*exceptions)


__all__ = ["RuntimeSovereign"]

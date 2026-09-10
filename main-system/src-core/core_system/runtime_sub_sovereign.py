"""Runtime Sub-Sovereign (system) — keeps the platform running, in-process.

Per the amended Governance Codex (A28 / E7, absorbed under the System
Sovereign; E127: ``RUNTIME-ACTION:system-runtime``), the Runtime
Sub-Sovereign owns the concerns required for the mother process to stay
alive and serve requests:

  * IPC server health / readiness contract
  * command surface (runtime bootstrap)
  * runtime status reporting
  * governance runtime integrity
  * system-wide hot-reload (runtime action, E127)

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
from governance_rule.codex import GOVERNANCE_CODEX

from governance_rule.execution.tool_runtime.sub_sovereign import (
    SYSTEM_RUNTIME_AUTHORITY,
)


_RUNTIME_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "system-runtime"),
    None,
)
if _RUNTIME_SOVEREIGN is None:
    raise RuntimeError("system-runtime sovereign not found in Governance Codex")

RUNTIME_SUB_SOVEREIGN_RESPONSIBILITIES = _RUNTIME_SOVEREIGN.duties


class RuntimeSubSovereign:
    """In-process sub-sovereign (under system) responsible for platform liveness and serving.

    Responsibilities:
      - Report readiness of the runtime command surface
      - Expose live runtime status (scope, phases, versions)
      - Track governance runtime integrity readiness
      - Coordinate system-wide hot-reload (E127: RUNTIME-ACTION:system-runtime)

    Idle memory maintenance is owned by the Resource Sub-Sovereign.
    """

    ROLE = _RUNTIME_SOVEREIGN.id

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

        Per E127 (``RUNTIME-ACTION:system-runtime``), the runtime
        sub-sovereign owns runtime actions including system-wide hot-reload.
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
    # System-wide hot-reload (E127: RUNTIME-ACTION:system-runtime)
    # ------------------------------------------------------------------

    async def _notify_ui(self, event: str, payload: dict[str, Any]) -> int:
        shells = getattr(self.app, "_active_ui_shells", None) or set()
        if not shells:
            return 0
        count = 0
        for shell in list(shells):
            send = getattr(shell, "send_event", None)
            if not callable(send):
                continue
            try:
                await send(event, payload)
                count += 1
            except Exception:
                pass
        return count

    async def execute_hot_reload(
        self,
        *,
        approval_token: str | None = None,
        modules: Any = None,
    ) -> dict[str, Any]:
        """Coordinate a system-wide hot-reload of governed backend modules.

        Per E127 (``RUNTIME-ACTION:system-runtime``), hot-reload is a runtime
        action owned by the runtime sub-sovereign.  It reloads already-loaded
        Python modules in-place so source edits to governed backend code take
        effect without a full process restart.  Governance authorization is
        required; the scope is system-wide (all backend src roots, not just
        main-system/src-core).

        After a successful reload the frontend is notified so it can refresh
        in sync.
        """
        hot_update = getattr(self.app, "hot_update_service", None)
        if hot_update is None:
            return {
                "ok": False,
                "duty": "runtime-action",
                "error": "hot-update-service-unavailable",
            }
        reload_modules = getattr(hot_update, "reload_modules", None)
        if not callable(reload_modules):
            return {
                "ok": False,
                "duty": "runtime-action",
                "error": "hot-reload-not-supported",
            }
        governance = getattr(self.app, "governance", None)
        report = await asyncio.to_thread(
            reload_modules,
            governance=governance,
            approval_token=approval_token,
            modules=modules,
        )

        # Sync the frontend so it can refresh against the newly loaded backend.
        notified = await self._notify_ui(
            "runtime:hot-reload-completed",
            {
                "ok": report.ok,
                "reloaded_count": len(report.reloaded),
                "skipped_count": len(report.skipped),
                "errors": list(report.errors)[:8],
            },
        )

        # A successful reload is an accepted runtime revision.  Automatic
        # repair must not run from this path or replace that accepted source.
        auto_repair: dict[str, Any] = {
            "ok": True,
            "skipped": True,
            "reason": "hot-reload-revision-protected",
        }

        return {
            "ok": report.ok,
            "duty": "runtime-action",
            "operation": "hot-reload",
            "authority": self.ROLE,
            "scope": "system-wide",
            "reloaded": list(report.reloaded),
            "skipped": list(report.skipped),
            "errors": list(report.errors),
            "ui_notified": notified,
            "auto_repair": auto_repair,
        }

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

__all__ = ["RUNTIME_SUB_SOVEREIGN_RESPONSIBILITIES", "RuntimeSubSovereign"]

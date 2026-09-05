"""Integration Sub-Sovereign (system) — owns ALL cross-sovereign-module structural interface concerns.

Per the Governance Codex (A32 / E19 / P15, absorbed under the System Sovereign),
the Integration Sub-Sovereign is responsible for every cross-sovereign and
cross-module structural interface, channel, synchronization and bus concerns.  It
is the sole owner of structural-interface matters; no other sovereign or module
may take them over (E20 boundary).

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates existing
interface executors and DELEGATES the actual interface operations to governed
executors; it never holds an execution power itself.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .codex_decision import decision_basis

INTEGRATION_SUB_SOVEREIGN_ROLE = "system-integration-sub-sovereign"

INTEGRATION_DECISION_AREA = "integration"


class IntegrationSubSovereign:
    """In-process sub-sovereign (under system) responsible for ALL cross-sovereign-module structural interface functions.

    Responsibilities (any structural-interface-related function):
      - cross-sovereign structural interfaces
      - cross-module structural interfaces
      - channel management
      - synchronization mechanisms
      - bus coordination
    """

    ROLE = INTEGRATION_SUB_SOVEREIGN_ROLE

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._supervision_task: asyncio.Task[Any] | None = None
        self._supervision_interval_seconds = 300.0
        self._command_router: Any | None = None
        self._toolbox: Any | None = None
        self._task_queue: Any | None = None
        self._channel_status_fn: Any | None = None
        self._bus_status_fn: Any | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        supervision_interval_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Start the Integration Sub-Sovereign and its in-process supervision loop.

        The sovereign captures the cross-module structural-interface governed
        executors from the app (decision-only supervision; it never executes
        the interface work in-process):
          ``command_router`` — cross-module command routing surface.
          ``toolbox`` — tool lifecycle bus / channel coordination.
          ``task_queue`` — the synchronization (begin/finish/cancel) mechanism.
          active IPC command tasks — channel / synchronization activity.
        """

        self._supervision_interval_seconds = max(60.0, float(supervision_interval_seconds))
        self._command_router = getattr(self.app, "command_router", None)
        self._toolbox = getattr(self.app, "toolbox_service", None)
        self._task_queue = getattr(self.app, "task_queue", None)
        self._started_at = self._iso_now()
        self._started = True

        if self._supervision_task is None:
            self._supervision_task = asyncio.create_task(
                self._supervision_loop(),
                name="system-integration-sub-sovereign-supervision",
            )

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "decision": decision_basis(INTEGRATION_DECISION_AREA),
        }

    async def stop(self) -> None:
        if self._supervision_task is not None:
            self._supervision_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._supervision_task
            self._supervision_task = None
        self._command_router = None
        self._toolbox = None
        self._task_queue = None
        self._channel_status_fn = None
        self._bus_status_fn = None
        self._started = False
        self._stopped_at = self._iso_now()

    # ------------------------------------------------------------------
    # Integration authority surface
    # ------------------------------------------------------------------

    def integration_status(self) -> dict[str, Any]:
        """Snapshot the structural-interface state."""

        decision = decision_basis(INTEGRATION_DECISION_AREA)
        return {
            "role": self.ROLE,
            "scope": "cross-sovereign-module-structural-interface",
            "cross_sovereign_interfaces": self._cross_sovereign_interfaces_status(),
            "cross_module_interfaces": self._cross_module_interfaces_status(),
            "channels": self._channels_status(),
            "synchronization": self._synchronization_status(),
            "bus": self._bus_status(),
            "decision": decision,
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "cross-sovereign-module-structural-interface",
            "started": self._started,
            "cross_sovereign_interfaces": self._cross_sovereign_interfaces_status(),
            "cross_module_interfaces": self._cross_module_interfaces_status(),
            "channels": self._channels_status(),
            "synchronization": self._synchronization_status(),
            "bus": self._bus_status(),
            "supervision_loop": {
                "running": self._supervision_task is not None and not self._supervision_task.done(),
                "interval_seconds": self._supervision_interval_seconds,
            },
            "decision": decision_basis(INTEGRATION_DECISION_AREA),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "name": "integration",
            "role": self.ROLE,
            "scope": "cross-sovereign-module-structural-interface",
            "state": "running" if self._started else "stopped",
            "delegation": "governed-executor-only",
            "serving": self._serving(),
            "active_channel_tasks": self._active_channel_tasks(),
            "synchronization": self._synchronization_status(),
            "decision": decision_basis(INTEGRATION_DECISION_AREA),
        }

    # ------------------------------------------------------------------
    # Integration-BODY responsibility status surfaces
    # ------------------------------------------------------------------

    def _serving(self) -> bool:
        router = self._command_router
        if router is None:
            return False
        return bool(getattr(router, "scope", None) == "main")

    def _active_channel_tasks(self) -> int:
        app = self.app
        if not hasattr(app, "_command_tasks"):
            return 0
        try:
            tasks = app._command_tasks
        except Exception:
            return 0
        if isinstance(tasks, dict):
            return len(tasks)
        return 0

    def _cross_sovereign_interfaces_status(self) -> dict[str, Any]:
        return {
            "duty": "cross-sovereign-interfaces",
            "delegation": "governed-executor-only",
            "decision": decision_basis(INTEGRATION_DECISION_AREA)["edicts"],
        }

    def _cross_module_interfaces_status(self) -> dict[str, Any]:
        toolbox_ok = self._toolbox is not None
        router_ok = self._command_router is not None
        return {
            "duty": "cross-module-interfaces",
            "tool_bus": toolbox_ok,
            "command_router": router_ok,
            "delegation": "governed-executor-only",
        }

    def _channels_status(self) -> dict[str, Any]:
        app = self.app
        governance = getattr(app, "governance", None)
        channel_capable = (
            governance is not None
            and hasattr(governance, "submit_tool_execution_request")
            and hasattr(governance, "tool_execution_response")
        )
        available_channels = []
        for name in ("system", "ai"):
            shared = app.project_root / "data" / f"{name}-channel.sqlite3"
            if shared.exists():
                available_channels.append(name)
        return {
            "duty": "channels",
            "channel_capable": channel_capable,
            "available_channels": available_channels,
            "static_artefact_channels": ["system", "ai"],
            "delegation": "governed-executor-only",
        }

    def _synchronization_status(self) -> dict[str, Any]:
        recovery: list[dict[str, Any]] = []
        queue = self._task_queue
        if queue is not None and hasattr(queue, "pending_recovery"):
            try:
                recovery = queue.pending_recovery() or []
            except Exception:
                recovery = []
        return {
            "duty": "synchronization",
            "active_channel_tasks": self._active_channel_tasks(),
            "pending_recovery": recovery,
            "delegation": "governed-executor-only",
        }

    def _bus_status(self) -> dict[str, Any]:
        toolbox = self._toolbox
        capability = (
            toolbox is not None
            and hasattr(toolbox, "request_tool_execution")
            and hasattr(toolbox, "cancel_tool_execution")
        )
        return {
            "duty": "bus",
            "request_capable": bool(capability),
            "delegation": "governed-executor-only",
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _supervision_loop(self) -> None:
        while self._started:
            await asyncio.sleep(self._supervision_interval_seconds)

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()


def _suppress(*exceptions: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(*exceptions)


__all__ = [
    "INTEGRATION_DECISION_AREA",
    "INTEGRATION_SOVEREIGN_ROLE",
    "IntegrationSubSovereign",
]

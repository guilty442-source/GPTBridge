"""Integration Sovereign — owns ALL information-channel governance concerns.

Per the amended Governance Codex, the integration sovereign
(``integration-sovereign`` / ``information-channel-sovereign``) is a
``specialized-decision-sovereign`` (A127) with three duties (A128):

  * govern-channel-topology     — govern the channel topology across the
                                  system (A32/A153/E50)
  * coordinate-message-routing  — coordinate message routing through the
                                  information layer (A65/A153/E50)
  * govern-cross-module-contracts — govern cross-module structural contracts
                                    (A32/E19)

Its powers (codex sovereign definition):
  * approve-channel-registration
  * decide-message-route
  * accept-interface-contract

Its prohibitions:
  * direct-ungoverned-execution
  * cross-sovereign-duty-takeover
  * permission-self-authorization

Authority boundaries:
  * A153 (supersedes A70): ``ALL-CHANNELS:information-layer-only`` — channels
    are exclusively owned and connected by the information layer.  The
    integration sovereign GOVERNS channel topology and coordinates routing;
    it does not own the channels themselves (A65/E50).
  * E19: ``EXEC:none; NO:decision-layer-coordinate`` — the sovereign is
    decision-only; it never executes interface work in-process.
  * E125 (supersedes E20): ``LEGACY-SYSTEM-SOVEREIGN:none`` — the old single
    system sovereign is gone; specialized sovereigns operate independently.
  * E107/A130: ``RETIRE:...integration...sub-sovereigns-after-promotion`` —
    the integration sub-sovereign has been promoted to a full
    specialized-decision-sovereign.  This module retains its class name for
    compatibility but operates under the codex sovereign identity
    ``integration-sovereign`` (manifest:
    ``sovereigns/information-channel-sovereign/manifest.json``).

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates existing
interface executors and DELEGATES the actual interface operations to governed
executors; it never holds an execution power itself (E19).

At startup it auto-starts only RESIDENT services (常駐服務) — modules whose
manifest declares ``lifecycle.stoppable: false``.  Non-resident services
(非常駐服務) are NOT started at system startup; they are started on demand
when the first execution request arrives (via the toolbox's on-demand start
in ``request_tool_execution``).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from .codex_decision import decision_basis
from .sovereign_utils import _iso_now, _suppress
from governance_rule.execution.tool_runtime.sub_sovereign import (
    SYSTEM_INTEGRATION_AUTHORITY,
)
from governance_rule.codex import GOVERNANCE_CODEX


_INTEGRATION_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "information-channel"),
    None,
)
if _INTEGRATION_SOVEREIGN is None:
    raise RuntimeError("integration sovereign not found in Governance Codex")

INTEGRATION_SUB_SOVEREIGN_RESPONSIBILITIES = _INTEGRATION_SOVEREIGN.duties

# Fallback resident service IDs used when manifest scanning is unavailable.
_FALLBACK_RESIDENT_TOOL_IDS = ("shared-layer",)

# Idle timeout: a module with no active execution request for this long is
# automatically stopped to conserve resources.  A subsequent request will
# auto-start it again via ensure_tool_running().  Resident services
# (lifecycle.stoppable == false) are exempt from idle stopping.
IDLE_TIMEOUT_SECONDS = float(__import__("os").environ.get("GPTBRIDGE_MODULE_IDLE_TIMEOUT", "300"))
IDLE_MONITOR_INTERVAL_SECONDS = 60.0


class IntegrationSubSovereign:
    """In-process sovereign responsible for ALL information-channel governance.

    Responsibilities (A128 — three codex duties):
      - govern-channel-topology (A32/A153/E50)
      - coordinate-message-routing (A65/A153/E50)
      - govern-cross-module-contracts (A32/E19)

    Powers (codex sovereign definition):
      - approve-channel-registration
      - decide-message-route
      - accept-interface-contract

    Per A153/E50, channels are information-layer-only; this sovereign
    GOVERNS topology and routing decisions but does not own the channels.
    Per E19, it is decision-only (EXEC:none) and never coordinates the
    decision layer itself.
    """

    ROLE = _INTEGRATION_SOVEREIGN.id

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._command_router: Any | None = None
        self._toolbox: Any | None = None
        self._task_queue: Any | None = None
        self._channel_status_fn: Any | None = None
        self._bus_status_fn: Any | None = None
        self._default_tool_startup: dict[str, dict[str, Any]] = {}
        self._default_tools_started = False
        # Idle module management
        self._tool_last_activity: dict[str, float] = {}
        self._idle_monitor_task: asyncio.Task[Any] | None = None
        self._idle_stopped_tools: set[str] = set()
        # Resident / non-resident classification
        self._resident_tool_ids: set[str] = set()
        self._non_resident_tool_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
    ) -> dict[str, Any]:
        """Start the Integration Sub-Sovereign.

        The sovereign captures the cross-module structural-interface governed
        executors from the app (decision-only supervision; it never executes
        the interface work in-process):
          ``command_router`` — cross-module command routing surface.
          ``toolbox`` — tool lifecycle bus / channel coordination.
          ``task_queue`` — the synchronization (begin/finish/cancel) mechanism.
          active IPC command tasks — channel / synchronization activity.
        """

        self._command_router = getattr(self.app, "command_router", None)
        self._toolbox = getattr(self.app, "toolbox_service", None)
        self._task_queue = getattr(self.app, "task_queue", None)
        self._started_at = _iso_now()
        self._started = True

        # Auto-start the governed default modules through the toolbox service.
        # The permission sovereign (started by the app before the system
        # sovereign) authorizes each module's lifecycle before start.
        await self._start_governed_default_tools()

        # Register the activity callback so the toolbox notifies us on every
        # tool execution request, resetting the idle timer.
        if self._toolbox is not None:
            self._toolbox._tool_activity_callback = self.mark_tool_activity

        # Start the idle-module monitor loop.
        if self._idle_monitor_task is None:
            self._idle_monitor_task = asyncio.create_task(
                self._idle_monitor_loop(),
                name="integration-sub-sovereign-idle-monitor",
            )

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "default_tools": dict(self._default_tool_startup),
            "decision": decision_basis(SYSTEM_INTEGRATION_AUTHORITY),
        }

    async def stop(self) -> None:
        if self._idle_monitor_task is not None:
            self._idle_monitor_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._idle_monitor_task
            self._idle_monitor_task = None
        self._command_router = None
        if self._toolbox is not None:
            self._toolbox._tool_activity_callback = None
        self._toolbox = None
        self._task_queue = None
        self._channel_status_fn = None
        self._bus_status_fn = None
        self._default_tools_started = False
        self._started = False
        self._stopped_at = _iso_now()

    # ------------------------------------------------------------------
    # Resident / non-resident classification + auto-start
    # ------------------------------------------------------------------

    def _classify_tools_by_manifest(self) -> None:
        """Scan tool manifests and classify each tool as resident or non-resident.

        A tool is RESIDENT (常駐) if its manifest declares
        ``lifecycle.stoppable: false``.  Resident services are auto-started
        at system startup and exempt from idle stopping.

        All other tools are NON-RESIDENT (非常駐) and are started on demand
        when the first execution request arrives.
        """

        project_root = Path(getattr(self.app, "project_root", Path.cwd()))
        self._resident_tool_ids.clear()
        self._non_resident_tool_ids.clear()

        for tool_dir in project_root.iterdir():
            manifest_path = tool_dir / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            tool_id = str(manifest.get("id") or tool_dir.name).strip()
            if not tool_id:
                continue
            lifecycle = manifest.get("lifecycle") or {}
            stoppable = lifecycle.get("stoppable")
            if stoppable is False:
                self._resident_tool_ids.add(tool_id)
            else:
                self._non_resident_tool_ids.add(tool_id)

        # Fallback: if no resident tools were found (manifest scan failed),
        # use the known resident service IDs.
        if not self._resident_tool_ids:
            self._resident_tool_ids = set(_FALLBACK_RESIDENT_TOOL_IDS)

    async def _start_governed_default_tools(self) -> None:
        """Auto-start only RESIDENT services (常駐服務) at system startup.

        Non-resident services (非常駐服務) are NOT started here; they start
        on demand when the first execution request arrives via
        ``toolbox.request_tool_execution()``.

        Each module's lifecycle must be authorized by the permission sovereign
        (started by the app before the system sovereign).  A permission denial
        is recorded but does not block the rest of startup (single-fault
        isolation).

        Different resident tools are started in parallel so that one tool's
        startup latency does not block another tool's startup.
        """

        if self._default_tools_started:
            return
        self._default_tools_started = True

        toolbox = self._toolbox
        if toolbox is None:
            return

        # Classify tools by manifest before starting.
        self._classify_tools_by_manifest()

        # Governance Authority is loaded in-process by Boot Core.  It is not
        # an independent child runtime and must never enter the toolbox
        # lifecycle path.
        if "governance_rule" in self._resident_tool_ids:
            self._resident_tool_ids.discard("governance_rule")
            self._default_tool_startup["governance_rule"] = {
                "ok": True,
                "runtime_mode": "in-process-authority",
                "error_code": "",
                "message": "Governance Authority already loaded",
                "resident": True,
            }

        permission = getattr(self.app, "permission_sovereign", None)

        async def _start_one(tool_id: str) -> tuple[str, dict[str, Any]]:
            if permission is None or not permission.can_start_tool(tool_id):
                return tool_id, {
                    "ok": False,
                    "tool_id": tool_id,
                    "error_code": "PERMISSION_DENIED",
                    "message": "PERMISSION_DENIED",
                }
            try:
                result = await toolbox.start_tool(
                    {
                        "tool_id": tool_id,
                        "request_id": f"resident-start-{tool_id}-{time.time_ns()}",
                        "background": True,
                    }
                )
            except Exception as error:
                result = {
                    "ok": False,
                    "tool_id": tool_id,
                    "error_code": type(error).__name__,
                    "message": str(error),
                }
            return tool_id, result

        # Start all resident tools in parallel — different tools have
        # independent process state slots (_active_request_by_tool is keyed
        # by tool_id), so concurrent startup is safe and avoids serial
        # latency where one slow tool blocks the next.
        results = await asyncio.gather(
            *(_start_one(tid) for tid in sorted(self._resident_tool_ids)),
            return_exceptions=False,
        )

        for tool_id, result in results:
            self._default_tool_startup[tool_id] = {
                "ok": result.get("ok") is True,
                "runtime_mode": str(result.get("runtime_mode") or ""),
                "error_code": str(result.get("error_code") or ""),
                "message": str(result.get("message") or ""),
                "resident": True,
            }
            if result.get("ok") is True:
                self._tool_last_activity[tool_id] = time.monotonic()
                self._idle_stopped_tools.discard(tool_id)

    # ------------------------------------------------------------------
    # Idle module management
    # ------------------------------------------------------------------

    def mark_tool_activity(self, tool_id: str) -> None:
        """Mark that a module received activity (execution request or start)."""

        self._tool_last_activity[tool_id] = time.monotonic()
        self._idle_stopped_tools.discard(tool_id)

    def is_tool_running(self, tool_id: str) -> bool:
        """Check whether a module is currently running (has a started process)."""

        toolbox = self._toolbox
        if toolbox is None:
            return False
        started = getattr(toolbox, "_started_request_by_tool", {})
        return tool_id in started

    async def ensure_tool_running(self, tool_id: str) -> bool:
        """Ensure a module is running; auto-start it if it was idle-stopped.

        Returns True if the module is running (or was successfully started),
        False otherwise.
        """

        if self.is_tool_running(tool_id):
            self.mark_tool_activity(tool_id)
            return True

        toolbox = self._toolbox
        if toolbox is None:
            return False

        permission = getattr(self.app, "permission_sovereign", None)
        if permission is None or not permission.can_start_tool(tool_id):
            return False

        try:
            result = await toolbox.start_tool(
                {
                    "tool_id": tool_id,
                    "request_id": f"idle-restart-{tool_id}-{time.time_ns()}",
                    "background": True,
                }
            )
        except Exception:
            return False

        if result.get("ok") is True:
            self.mark_tool_activity(tool_id)
            return True
        return False

    async def _idle_monitor_loop(self) -> None:
        """Periodically check for idle modules and stop them.

        A module is considered idle if it has a started process but no active
        execution request, and the last activity timestamp is older than
        IDLE_TIMEOUT_SECONDS.  Stopped modules are recorded in
        ``_idle_stopped_tools`` so that ``ensure_tool_running`` can restart
        them on demand.
        """

        while self._started:
            await asyncio.sleep(IDLE_MONITOR_INTERVAL_SECONDS)
            if not self._started:
                break
            await self._check_idle_tools()

    async def _check_idle_tools(self) -> None:
        toolbox = self._toolbox
        if toolbox is None:
            return

        started_by_tool: dict[str, str] = getattr(
            toolbox, "_started_request_by_tool", {}
        )
        active_by_tool: dict[str, str] = getattr(
            toolbox, "_active_request_by_tool", {}
        )
        now = time.monotonic()

        # Collect idle tools that need to be stopped.
        idle_tool_ids: list[str] = []
        for tool_id in list(started_by_tool.keys()):
            # Resident services (常駐服務) are never idle-stopped.
            if tool_id in self._resident_tool_ids:
                continue

            # Skip modules with an active execution request.
            if tool_id in active_by_tool:
                self._tool_last_activity[tool_id] = now
                continue

            last_activity = self._tool_last_activity.get(tool_id)
            if last_activity is None:
                # No activity recorded — treat start time as last activity.
                self._tool_last_activity[tool_id] = now
                continue

            idle_seconds = now - last_activity
            if idle_seconds < IDLE_TIMEOUT_SECONDS:
                continue

            # Idle timeout reached — candidate for stopping.
            permission = getattr(self.app, "permission_sovereign", None)
            if permission is None or not permission.can_start_tool(tool_id):
                continue
            idle_tool_ids.append(tool_id)

        # Stop all idle tools in parallel — different tools have independent
        # process state slots, so concurrent stop is safe.
        async def _stop_one(tid: str) -> str | None:
            try:
                await toolbox.stop_tool({"tool_id": tid})
            except Exception:
                return None
            return tid

        stopped = await asyncio.gather(
            *(_stop_one(tid) for tid in idle_tool_ids),
            return_exceptions=False,
        )
        for tid in stopped:
            if tid is None:
                continue
            self._idle_stopped_tools.add(tid)
            self._tool_last_activity.pop(tid, None)

    # ------------------------------------------------------------------
    # Integration authority surface
    # ------------------------------------------------------------------

    def integration_status(self) -> dict[str, Any]:
        """Snapshot the information-channel governance state (A128)."""

        decision = decision_basis(SYSTEM_INTEGRATION_AUTHORITY)
        return {
            "role": self.ROLE,
            "scope": "information-channel-governance",
            "channel_topology": self._channel_topology_status(),
            "message_routing": self._message_routing_status(),
            "cross_module_contracts": self._cross_module_contracts_status(),
            "decision": decision,
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "information-channel-governance",
            "started": self._started,
            "channel_topology": self._channel_topology_status(),
            "message_routing": self._message_routing_status(),
            "cross_module_contracts": self._cross_module_contracts_status(),
            "default_tools": dict(self._default_tool_startup),
            "resident_tools": sorted(self._resident_tool_ids),
            "non_resident_tools": sorted(self._non_resident_tool_ids),
            "idle_management": {
                "enabled": self._idle_monitor_task is not None,
                "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
                "idle_stopped_tools": list(self._idle_stopped_tools),
                "monitored_tools": list(self._tool_last_activity.keys()),
                "resident_exempt": True,
            },
            "decision": decision_basis(SYSTEM_INTEGRATION_AUTHORITY),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "name": "integration",
            "role": self.ROLE,
            "scope": "information-channel-governance",
            "state": "running" if self._started else "stopped",
            "delegation": "governed-executor-only",
            "serving": self._serving(),
            "active_channel_tasks": self._active_channel_tasks(),
            "default_tools": dict(self._default_tool_startup),
            "resident_tools": sorted(self._resident_tool_ids),
            "non_resident_tools": sorted(self._non_resident_tool_ids),
            "idle_management": {
                "enabled": self._idle_monitor_task is not None,
                "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
                "idle_stopped_tools": list(self._idle_stopped_tools),
                "resident_exempt": True,
            },
            "message_routing": self._message_routing_status(),
            "decision": decision_basis(SYSTEM_INTEGRATION_AUTHORITY),
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

    def _channel_topology_status(self) -> dict[str, Any]:
        """govern-channel-topology (A32/A153/E50) — channel topology governance.

        Per A153 (supersedes A70): ``ALL-CHANNELS:information-layer-only``.
        The integration sovereign GOVERNS channel topology; the channels
        themselves are owned by the information layer (A65/E50).
        """
        return {
            "duty": "govern-channel-topology",
            "cross_sovereign_interfaces": self._cross_sovereign_interfaces_status(),
            "bus": self._bus_status(),
            "channel_authority": "information-layer-only",
            "delegation": "governed-executor-only",
            "decision": decision_basis(SYSTEM_INTEGRATION_AUTHORITY)["edicts"],
        }

    def _message_routing_status(self) -> dict[str, Any]:
        """coordinate-message-routing (A65/A153/E50) — message routing coordination.

        Per A153/E50: ``ROUTE:sender>information-layer>authorized-destination``.
        The integration sovereign COORDINATES routing; the information layer
        owns the channels.
        """
        return {
            "duty": "coordinate-message-routing",
            "channels": self._channels_status(),
            "synchronization": self._synchronization_status(),
            "active_channel_tasks": self._active_channel_tasks(),
            "delegation": "governed-executor-only",
        }

    def _cross_module_contracts_status(self) -> dict[str, Any]:
        """govern-cross-module-contracts (A32/E19) — cross-module contract governance.

        Per E19: ``EXEC:none; NO:decision-layer-coordinate`` — the sovereign
        governs contracts but does not execute or coordinate the decision
        layer.
        """
        return {
            "duty": "govern-cross-module-contracts",
            "cross_module_interfaces": self._cross_module_interfaces_status(),
            "delegation": "governed-executor-only",
        }

    def _cross_sovereign_interfaces_status(self) -> dict[str, Any]:
        return {
            "duty": "govern-channel-topology",
            "delegation": "governed-executor-only",
            "decision": decision_basis(SYSTEM_INTEGRATION_AUTHORITY)["edicts"],
        }

    def _cross_module_interfaces_status(self) -> dict[str, Any]:
        toolbox_ok = self._toolbox is not None
        router_ok = self._command_router is not None
        return {
            "duty": "govern-cross-module-contracts",
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
            "duty": "coordinate-message-routing",
            "channel_capable": channel_capable,
            "available_channels": available_channels,
            "static_artefact_channels": ["system", "ai"],
            "channel_authority": "information-layer-only",
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
            "duty": "coordinate-message-routing",
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
            "duty": "govern-channel-topology",
            "request_capable": bool(capability),
            "delegation": "governed-executor-only",
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

__all__ = [
    "INTEGRATION_SUB_SOVEREIGN_RESPONSIBILITIES",
    "IntegrationSubSovereign",
]

"""Channel Contract Sync Sub-Sovereign — Channel Contract Registry and Status Surfaces."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .._base import SubSovereignBase
from core_system.codex_decision import decision_basis
from core_system.sovereign_utils import _iso_now, _suppress
from governance_rule.execution.tool_runtime.sub_sovereign import (
    SYSTEM_INTEGRATION_AUTHORITY,
)
from governance_rule.execution.codex_official import official_self_declaration


_INTEGRATION_SOVEREIGN = official_self_declaration("channel-contract-sync-sub-sovereign")
if _INTEGRATION_SOVEREIGN is None:
    raise RuntimeError("channel contract sync sub-sovereign not found in Governance Codex")


class ContractRegistryMixin:
    """Channel contract registry and status surfaces."""

    _contracts: dict[str, dict[str, Any]]
    _command_router: Any
    _toolbox: Any
    _task_queue: Any
    _channel_status_fn: Any
    _bus_status_fn: Any
    _default_tool_startup: dict[str, dict[str, Any]]
    _resident_tool_ids: set[str]
    _non_resident_tool_ids: set[str]
    _sync_state: dict[str, Any]
    _idle_stopped_tools: set[str]
    _tool_last_activity: dict[str, float]
    _idle_monitor_task: asyncio.Task[Any] | None
    _started: bool
    _started_at: str | None
    _stopped_at: str | None
    app: Any
    ROLE: str
    parent_sovereign_id: str

    def register_contract(self, channel_id: str, contract: dict[str, Any]) -> bool:
        """Register channel contract — fail-closed coordination."""
        if not channel_id or not isinstance(contract, dict):
            return False
        existing = self._contracts.get(channel_id)
        if existing is not None and existing.get("contract") != contract:
            return False
        self._contracts[channel_id] = {
            "contract": contract,
            "registered_at": self._iso_now(),
            "status": "active",
        }
        return True

    def integration_status(self) -> dict[str, Any]:
        """Snapshot the information-channel governance state."""
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
            "parent": self.parent_sovereign_id,
            "started": self._started,
            "channel_topology": self._channel_topology_status(),
            "message_routing": self._message_routing_status(),
            "cross_module_contracts": self._cross_module_contracts_status(),
            "contracts": list(self._contracts.keys()),
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
            "sync_state": self._sync_state,
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
        except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
            return 0
        if isinstance(tasks, dict):
            return len(tasks)
        return 0

    def _channel_topology_status(self) -> dict[str, Any]:
        """govern-channel-topology (A32/A153/E50)."""
        return {
            "duty": "govern-channel-topology",
            "cross_sovereign_interfaces": self._cross_sovereign_interfaces_status(),
            "bus": self._bus_status(),
            "channel_authority": "information-layer-only",
            "delegation": "governed-executor-only",
            "decision": decision_basis(SYSTEM_INTEGRATION_AUTHORITY)["edicts"],
        }

    def _message_routing_status(self) -> dict[str, Any]:
        """coordinate-message-routing (A65/A153/E50)."""
        return {
            "duty": "coordinate-message-routing",
            "channels": self._channels_status(),
            "synchronization": self._synchronization_status(),
            "active_channel_tasks": self._active_channel_tasks(),
            "delegation": "governed-executor-only",
        }

    def _cross_module_contracts_status(self) -> dict[str, Any]:
        """govern-cross-module-contracts (A32/E19)."""
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
            except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
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
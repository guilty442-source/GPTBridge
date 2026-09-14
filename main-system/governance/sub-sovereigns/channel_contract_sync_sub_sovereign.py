"""Channel Contract Sync Sub-Sovereign — 通道契約同步子主權（子屬同步主宰，無決策、無執行）。

法典依據:
- sovereign_id: channel-contract-sync-sub-sovereign (position 30)
- area: information-channel
- rank: child-of-synchronization-sovereign-no-decision-no-execution
- basis: A306/A322 (retires integration-sovereign / integration-sub-sovereign)

The implementation was merged from the retired
``core_system.integration_sub_sovereign.IntegrationSubSovereign`` so the
active path keeps its channel-topology / message-routing / cross-module
contract supervision and the resident-tool idle-management behavior while
operating under the codex ``channel-contract-sync-sub-sovereign`` identity.

Authority boundaries:
  * A153 (supersedes A70): ``ALL-CHANNELS:information-layer-only`` — channels
    are exclusively owned and connected by the information layer.  This
    sub-sovereign GOVERNS channel topology and coordinates routing; it does
    not own the channels themselves (A65/E50).
  * E19: ``EXEC:none; NO:decision-layer-coordinate`` — decision-only; it
    never executes interface work in-process.

At startup it auto-starts only RESIDENT services (常駐服務) — modules whose
manifest declares ``lifecycle.stoppable: false``.  Non-resident services
(非常駐服務) are NOT started at system startup; they are started on demand
when the first execution request arrives (via the toolbox's on-demand start
in ``request_tool_execution``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ._base import SubSovereignBase
from core_system.codex_decision import decision_basis
from core_system.sovereign_utils import _iso_now, _suppress
from governance_rule.execution.tool_runtime.sub_sovereign import (
    SYSTEM_INTEGRATION_AUTHORITY,
)
from governance_rule.execution.codex_official import official_sovereign

from .channel_contract.tool_classification import ToolClassificationMixin
from .channel_contract.idle_management import IdleManagementMixin
from .channel_contract.contract_status import ContractRegistryMixin

_logger = logging.getLogger("gptbridge.sub_sovereign.channel_contract")

_INTEGRATION_SOVEREIGN = official_sovereign("channel-contract-sync-sub-sovereign", requester="channel-contract-sync-sub-sovereign", purpose="self-declaration")
if _INTEGRATION_SOVEREIGN is None:
    raise RuntimeError("channel contract sync sub-sovereign not found in Governance Codex")

# Fallback resident service IDs used when manifest scanning is unavailable.
_FALLBACK_RESIDENT_TOOL_IDS = ("shared-layer",)

# Idle timeout: a module with no active execution request for this long is
# automatically stopped to conserve resources.  A subsequent request will
# auto-start it again via ensure_tool_running().  Resident services
# (lifecycle.stoppable == false) are exempt from idle stopping.
IDLE_TIMEOUT_SECONDS = float(__import__("os").environ.get("GPTBRIDGE_MODULE_IDLE_TIMEOUT", "300"))
IDLE_MONITOR_INTERVAL_SECONDS = 60.0


class ChannelContractSyncSubSovereign(
    SubSovereignBase,
    ToolClassificationMixin,
    IdleManagementMixin,
    ContractRegistryMixin,
):
    """In-process sub-sovereign responsible for information-channel governance."""

    sovereign_id = "channel-contract-sync-sub-sovereign"
    parent_sovereign_id = "synchronization-sovereign"

    ROLE = sovereign_id

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._command_router: Any | None = None
        self._toolbox: Any | None = None
        self._task_queue: Any | None = None
        self._channel_status_fn: Any | None = None
        self._bus_status_fn: Any | None = None
        self._default_tool_startup: dict[str, dict[str, Any]] = {}
        self._default_tools_started = False
        self._sync_state: dict[str, Any] = {}
        self._tool_last_activity: dict[str, float] = {}
        self._idle_monitor_task: asyncio.Task[Any] | None = None
        self._default_tools_task: asyncio.Task[Any] | None = None
        self._idle_stopped_tools: set[str] = set()
        self._resident_tool_ids: set[str] = set()
        self._non_resident_tool_ids: set[str] = set()
        self._contracts: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Start the Channel Contract Sync Sub-Sovereign."""
        self._command_router = getattr(self.app, "command_router", None)
        self._toolbox = getattr(self.app, "toolbox_service", None)
        self._task_queue = getattr(self.app, "task_queue", None)
        self._started_at = _iso_now()
        self._started = True

        if self._default_tools_task is None:
            self._default_tools_task = asyncio.create_task(
                self._start_governed_default_tools(),
                name="channel-contract-sync-default-tools",
            )

        if self._toolbox is not None:
            self._toolbox._tool_activity_callback = self.mark_tool_activity

        await self.start_idle_monitor()

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "default_tools": dict(self._default_tool_startup),
            "decision": decision_basis(SYSTEM_INTEGRATION_AUTHORITY),
        }

    async def stop(self) -> None:
        if self._default_tools_task is not None:
            self._default_tools_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._default_tools_task
            self._default_tools_task = None
        await self.stop_idle_monitor()
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


__all__ = [
    "ChannelContractSyncSubSovereign",
    "IDLE_MONITOR_INTERVAL_SECONDS",
    "IDLE_TIMEOUT_SECONDS",
]
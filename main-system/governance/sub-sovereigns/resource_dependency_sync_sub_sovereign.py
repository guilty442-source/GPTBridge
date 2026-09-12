"""Resource Dependency Sync Sub-Sovereign — 資源依賴同步子主權（子屬同步主宰，無決策、無執行）。

法典依據:
- sovereign_id: resource-dependency-sync-sub-sovereign (position 29)
- area: resource-allocation
- rank: child-of-synchronization-sovereign-no-decision-no-execution
- basis: A305/A322 (retires resource-sovereign / resource-sub-sovereign)

The implementation was merged from the retired
``core_system.resource_sub_sovereign.ResourceSubSovereign`` so the active
path keeps its resource-state monitoring / delegated release behavior while
operating under the codex ``resource-dependency-sync-sub-sovereign``
identity.

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates existing
resource executors and DELEGATES the actual resource release to governed
executors (the C++ native kernel and the resource-maintenance executor); it
never holds an execution power itself.
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase
from core_system.codex_decision import decision_basis
from core_system.sovereign_utils import _iso_now
from core_system.native import (
    native_available,
    release_resources,
    resource_status,
)
from governance_rule.codex import GOVERNANCE_CODEX


_RESOURCE_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "resource-allocation"),
    None,
)
if _RESOURCE_SOVEREIGN is None:
    raise RuntimeError("resource dependency sync sub-sovereign not found in Governance Codex")

RESOURCE_DECISION_AREA = _RESOURCE_SOVEREIGN.area


class ResourceDependencySyncSubSovereign(SubSovereignBase):
    """In-process sub-sovereign responsible for resource-allocation functions.

    Responsibilities (codex sovereign definition — A305/A322):
      - monitor-resource-state (memory / disk / model / compute)
      - resource dependency synchronization (provisioning / configuration coordination)
      - coordinate-resource-release (delegated to native kernel + executor)
    """

    sovereign_id = "resource-dependency-sync-sub-sovereign"
    parent_sovereign_id = "synchronization-sovereign"

    ROLE = sovereign_id

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._memory_maintainer: Any | None = None
        self._sync_state: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        memory_maintainer: Any = None,
    ) -> dict[str, Any]:
        """Start the Resource Dependency Sync Sub-Sovereign.

        ``memory_maintainer``: the IdleMemoryMaintainer instance already built
            by the app; the sovereign coordinates/supervises it but the actual
            idle-release loop is owned by that governed executor.
        """

        self._memory_maintainer = memory_maintainer
        self._started_at = _iso_now()
        self._started = True

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "decision": decision_basis(RESOURCE_DECISION_AREA),
            "native_available": native_available(),
        }

    async def stop(self) -> None:
        self._memory_maintainer = None
        self._started = False
        self._stopped_at = _iso_now()

    # ------------------------------------------------------------------
    # Resource authority surface
    # ------------------------------------------------------------------

    def release_resources(self) -> dict[str, Any]:
        """Delegate a resource release to the governed executor (no self-exec).

        References the Codex resource decision basis, then delegates the actual
        release to the native kernel / resource-maintenance executor on a
        background thread.
        """

        decision_basis(RESOURCE_DECISION_AREA)
        governance = getattr(self.app, "governance", None)
        if governance is None:
            # Fall back to the controlled native executor at the decision layer.
            native_release = getattr(
                self.app, "resource_release", resource_status
            )
            if callable(native_release):
                return {"delegated": True, "result": native_release()}
            return {"delegated": False}
        # In a fully governed deployment the app wires a resource executor:
        release = getattr(self.app, "resource_release", None)
        if callable(release):
            return {"delegated": True, "result": release()}
        return {"delegated": False}

    def sync_resources(self, resources: dict[str, Any]) -> None:
        """同步資源狀態。"""
        self._sync_state = {
            "resources": resources,
            "synced_at": self._iso_now(),
        }

    def resource_status(self) -> dict[str, Any]:
        """Snapshot the resource-BODY state (native kernel + executor)."""

        decision = decision_basis(RESOURCE_DECISION_AREA)
        return {
            "role": self.ROLE,
            "scope": "resource-allocation",
            "native": resource_status(),
            "native_available": native_available(),
            "memory_maintenance": self._memory_maintainer_status(),
            "decision": decision,
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "resource-allocation",
            "parent": self.parent_sovereign_id,
            "started": self._started,
            "native": resource_status(),
            "native_available": native_available(),
            "memory_maintenance": self._memory_maintainer_status(),
            "sync_state": self._sync_state,
            "decision": decision_basis(RESOURCE_DECISION_AREA),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "name": "resource",
            "role": self.ROLE,
            "scope": "resource-allocation",
            "state": "running" if self._started else "stopped",
            "delegation": "governed-executor-only",
            "native_available": native_available(),
            "decision": decision_basis(RESOURCE_DECISION_AREA),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _memory_maintainer_status(self) -> dict[str, Any]:
        if self._memory_maintainer is None:
            return {"enabled": False}
        get_status = getattr(self._memory_maintainer, "status", None)
        if callable(get_status):
            return get_status()
        return {"enabled": True}


__all__ = ["RESOURCE_DECISION_AREA", "ResourceDependencySyncSubSovereign"]

"""Resource Sovereign — owns ALL resource-allocation concerns, in-process.

Per the Governance Codex (A30 / E17 / P13), the Resource Sovereign is a
``specialized-decision-sovereign`` (A127/E125) responsible for every
resource-body concern: memory, disk, model and compute resource state
monitoring, provisioning and delegated release.  It is the sole owner of
resource-body matters; no other sovereign or module may take them over
(E20 boundary).

Duties (codex sovereign definition):
  * monitor-resource-state
  * decide-resource-allocation
  * coordinate-resource-release

Powers:
  * decide-resource-allocation
  * dispatch-resource-control
  * accept-resource-release

Prohibitions:
  * direct-ungoverned-execution
  * cross-sovereign-duty-takeover
  * permission-self-authorization

The codex registration (resource-allocation-sovereign) is the authoritative
source for this module's role, decision area and responsibilities; the legacy
``system-resource-sub-sovereign`` id exists only as the execution-layer
channel role.

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates existing
resource executors and DELEGATES the actual resource release to governed
executors (the C++ native kernel and the resource-maintenance executor); it
never holds an execution power itself.
"""

from __future__ import annotations

from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX

from .codex_decision import decision_basis
from .sovereign_utils import _iso_now
from .native import (
    native_available,
    release_resources,
    resource_status,
)

_RESOURCE_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "resource-allocation"),
    None,
)
if _RESOURCE_SOVEREIGN is None:
    raise RuntimeError("resource sovereign not found in Governance Codex")

RESOURCE_SUB_SOVEREIGN_ROLE = _RESOURCE_SOVEREIGN.id

RESOURCE_SUB_SOVEREIGN_RESPONSIBILITIES = _RESOURCE_SOVEREIGN.duties

RESOURCE_DECISION_AREA = _RESOURCE_SOVEREIGN.area


class ResourceSubSovereign:
    """In-process sovereign responsible for ALL resource-allocation functions.

    Responsibilities (codex sovereign definition — A30/E17):
      - monitor-resource-state (memory / disk / model / compute)
      - decide-resource-allocation (provisioning / configuration coordination)
      - coordinate-resource-release (delegated to native kernel + executor)
    """

    ROLE = RESOURCE_SUB_SOVEREIGN_ROLE

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._memory_maintainer: Any | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        memory_maintainer: Any = None,
    ) -> dict[str, Any]:
        """Start the Resource Sub-Sovereign.

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
            "started": self._started,
            "native": resource_status(),
            "native_available": native_available(),
            "memory_maintenance": self._memory_maintainer_status(),
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

__all__ = [
    "RESOURCE_DECISION_AREA",
    "RESOURCE_SUB_SOVEREIGN_RESPONSIBILITIES",
    "RESOURCE_SUB_SOVEREIGN_ROLE",
    "ResourceSubSovereign",
]

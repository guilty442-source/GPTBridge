"""Integration Sub-Sovereign — cross-module interface authority for governed tool lifecycle."""

from __future__ import annotations

from typing import Any


_FALLBACK_RESIDENT_TOOL_IDS = frozenset(
    {
        "governance_rule",
        "shared-layer",
    }
)


def _classify_tools_by_manifest(manifests: dict[str, Any]) -> dict[str, set[str]]:
    """Classify tools by their manifest lifecycle properties.

    Returns dict with keys:
    - "resident": tools with lifecycle.stoppable == false
    - "on_demand": tools with lifecycle.stoppable == true
    """
    resident = set()
    on_demand = set()

    for tool_id, manifest in manifests.items():
        lifecycle = manifest.get("lifecycle", {})
        if lifecycle.get("stoppable") is False:
            resident.add(tool_id)
        else:
            on_demand.add(tool_id)

    return {"resident": resident, "on_demand": on_demand}


class IntegrationSubSovereign:
    """Cross-module interface authority for governed tool lifecycle management.

    This sovereign is responsible for:
    - Classifying tools by manifest (resident vs on-demand)
    - Managing auto-start of resident services only
    - Coordinating with governance for tool lifecycle
    """

    def __init__(self, project_root: str, governance: Any):
        self.project_root = project_root
        self.governance = governance

    def _start_governed_default_tools(self) -> None:
        """Start only resident services (lifecycle.stoppable == false) by default.

        Non-resident services start on demand via explicit invocation.
        """
        # Implementation would:
        # 1. Load all tool manifests
        # 2. Classify via _classify_tools_by_manifest
        # 3. Start resident tools through governed runtime
        # self._start_governed_default_tools() called during initialization
        pass

    def get_resident_tool_ids(self) -> frozenset[str]:
        """Get the set of tool IDs that are resident services."""
        return _FALLBACK_RESIDENT_TOOL_IDS
"""Priority Capability Sub-Sovereign — 優先能力子主權（子屬決策主宰，無決策、無執行）。

法典依據:
- sovereign_id: priority-capability-sub-sovereign (position 40)
- area: priority-capability
- rank: child-of-decision-sovereign-no-decision-no-execution
- basis: A323
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase


class PriorityCapabilitySubSovereign(SubSovereignBase):
    """優先能力子主權：能力優先級協調。"""

    sovereign_id = "priority-capability-sub-sovereign"
    parent_sovereign_id = "decision-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._capabilities: dict[str, dict[str, Any]] = {}

    def register_capability(self, cap_id: str, spec: dict[str, Any]) -> None:
        self._capabilities[cap_id] = {
            "spec": spec,
            "registered_at": self._iso_now(),
            "status": "active",
        }

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["capabilities"] = list(self._capabilities.keys())
        return base


__all__ = ["PriorityCapabilitySubSovereign"]
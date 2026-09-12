"""Policy Architecture Sub-Sovereign — 政策架構子主權（子屬決策主宰，無決策、無執行）。

法典依據:
- sovereign_id: policy-architecture-sub-sovereign (position 37)
- area: policy-architecture
- rank: child-of-decision-sovereign-no-decision-no-execution
- basis: A323
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase


class PolicyArchitectureSubSovereign(SubSovereignBase):
    """政策架構子主權：政策架構協調。"""

    sovereign_id = "policy-architecture-sub-sovereign"
    parent_sovereign_id = "decision-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._policies: dict[str, dict[str, Any]] = {}

    def register_policy(self, policy_id: str, spec: dict[str, Any]) -> None:
        self._policies[policy_id] = {
            "spec": spec,
            "registered_at": self._iso_now(),
            "status": "active",
        }

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["policies"] = list(self._policies.keys())
        return base


__all__ = ["PolicyArchitectureSubSovereign"]
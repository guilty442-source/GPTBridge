"""Identity Group Sub-Sovereign — 身份群組子主權（子屬權限主宰，無決策、無審查、無執行）。

法典依據:
- sovereign_id: identity-group-sub-sovereign (position 28)
- area: identity-group-management
- rank: child-of-permission-sovereign-no-decision-no-review-no-execution
- basis: A317
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase


class IdentityGroupSubSovereign(SubSovereignBase):
    """身份群組子主權：群組成員/衝突/巢狀/撤銷管理。"""

    sovereign_id = "identity-group-sub-sovereign"
    parent_sovereign_id = "permission-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._groups: dict[str, dict[str, Any]] = {}

    def register_group(self, group_id: str, members: list[str], spec: dict[str, Any]) -> None:
        self._groups[group_id] = {
            "members": members,
            "spec": spec,
            "registered_at": self._iso_now(),
            "status": "active",
        }

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        return self._groups.get(group_id)

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["groups"] = list(self._groups.keys())
        return base


__all__ = ["IdentityGroupSubSovereign"]
"""System Sub-Sovereign — 系統子主權（模組管理/指派/協調，子屬運行主宰）。

法典依據:
- sovereign_id: system-sub-sovereign (position 15)
- area: system-module-management-assignment-coordination
- rank: child-of-runtime-sovereign-module-management-assignment-coordination-no-decision-no-execution
- basis: A284|A287
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase


class SystemSubSovereign(SubSovereignBase):
    """系統子主權：模組管理/指派/協調。"""

    sovereign_id = "system-sub-sovereign"
    parent_sovereign_id = "system-runtime-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._modules: dict[str, dict[str, Any]] = {}

    async def start(self) -> dict[str, Any]:
        state = await super().start()
        state["modules"] = list(self._modules.keys())
        return state

    def register_module(self, module_id: str, config: dict[str, Any]) -> bool:
        """A284/A334: only modules assigned to this sub-sovereign in the
        codex module_assignment_registry may be managed here — anything
        else is refused (fail-closed coordination)."""
        from ..registries import module_assignment

        row = module_assignment(module_id)
        if row is None or row.get("managing_sub_sovereign") != self.sovereign_id:
            return False
        self._modules[module_id] = {
            "config": config,
            "primary_domain": row.get("primary_domain"),
            "registered_at": self._iso_now(),
            "status": "registered",
        }
        return True

    def get_module(self, module_id: str) -> dict[str, Any] | None:
        return self._modules.get(module_id)

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["modules"] = {
            k: {"status": v["status"], "registered_at": v["registered_at"]}
            for k, v in self._modules.items()
        }
        return base


__all__ = ["SystemSubSovereign"]
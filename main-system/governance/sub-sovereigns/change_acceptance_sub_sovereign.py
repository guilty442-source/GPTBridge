"""Change Acceptance Sub-Sovereign — 變更接受子主權（子屬決策主宰，無決策、無執行）。

法典依據:
- sovereign_id: change-acceptance-sub-sovereign (position 41)
- area: change-acceptance
- rank: child-of-decision-sovereign-no-decision-no-execution
- basis: A323
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase


class ChangeAcceptanceSubSovereign(SubSovereignBase):
    """變更接受子主權：變更驗收/驗證協調。"""

    sovereign_id = "change-acceptance-sub-sovereign"
    parent_sovereign_id = "decision-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._changes: dict[str, dict[str, Any]] = {}

    def submit_change(self, change_id: str, spec: dict[str, Any]) -> bool:
        """A323: change-acceptance coordination — refuse malformed or
        conflicting submissions (fail-closed; no decision power)."""
        if not change_id or not isinstance(spec, dict):
            return False
        existing = self._changes.get(change_id)
        if existing is not None and existing.get("spec") != spec:
            return False
        self._changes[change_id] = {
            "spec": spec,
            "submitted_at": self._iso_now(),
            "status": "pending",
        }
        return True

    def accept_change(self, change_id: str, result: dict[str, Any]) -> bool:
        """Record acceptance of a submitted change — only pending changes
        may be accepted (fail-closed coordination)."""
        change = self._changes.get(change_id)
        if change is None or change.get("status") != "pending":
            return False
        change.update({
            "result": result,
            "accepted_at": self._iso_now(),
            "status": "accepted",
        })
        return True

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["changes"] = {
            k: {"status": v["status"], "submitted_at": v["submitted_at"]}
            for k, v in self._changes.items()
        }
        return base


__all__ = ["ChangeAcceptanceSubSovereign"]
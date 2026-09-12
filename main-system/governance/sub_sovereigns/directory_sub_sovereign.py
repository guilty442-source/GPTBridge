"""Directory Sub-Sovereign — 目錄子主權（子屬權限主宰，無決策、無審查、無執行）。

法典依據:
- sovereign_id: directory-sub-sovereign (position 27)
- area: directory-management
- rank: child-of-permission-sovereign-no-decision-no-review-no-execution
- basis: A316
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase


class DirectorySubSovereign(SubSovereignBase):
    """目錄子主權：目錄管理（A42/A278/A280/A283）。"""

    sovereign_id = "directory-sub-sovereign"
    parent_sovereign_id = "permission-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._directories: dict[str, dict[str, Any]] = {}

    def register_directory(self, dir_id: str, spec: dict[str, Any]) -> None:
        self._directories[dir_id] = {
            "spec": spec,
            "registered_at": self._iso_now(),
            "status": "active",
        }

    def get_directory(self, dir_id: str) -> dict[str, Any] | None:
        return self._directories.get(dir_id)

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["directories"] = list(self._directories.keys())
        return base


__all__ = ["DirectorySubSovereign"]
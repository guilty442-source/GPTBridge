"""system executors — 系統編排執行器（輕量，受委派觸發）。"""

from __future__ import annotations

import sys
from typing import Any

from ..governance.delegation import ExecutorBinding


def _lifecycle_milestone(payload: dict[str, Any]) -> dict[str, Any]:
    structure = payload.get("structure") or {}
    return {
        "milestone": "lifecycle-recorded",
        "stages": structure.get("stages", 0),
        "recorded_by": "lifecycle-milestone",
        "host": sys.platform,
    }


def bindings() -> list[ExecutorBinding]:
    return [
        ExecutorBinding(
            executor_id="lifecycle-milestone",
            boundary="full-lifecycle-orchestration",
            permission_intent="lifecycle-milestone",
            owner_sovereign="system",
            target="system-sovereign:delegate:lifecycle-milestone",
            implementation=_lifecycle_milestone,
        ),
    ]


__all__ = ["bindings"]
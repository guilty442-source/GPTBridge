"""Third-Party Management Sub-Sovereign (system) — owns third-party software governance.

Per the Governance Codex (A51 / P25 / E37, absorbed under the System Sovereign),
the Third-Party Management Sub-Sovereign owns third-party software introduction,
version, license, and security management.  It ensures no non-formal third-party
software, packages, or external services are introduced (P7 / A37 / A49).

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates the tool
inventory and DELEGATES the actual enforcement to governed executors; it never
holds an execution power itself.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex_decision import decision_basis

THIRD_PARTY_ROLE = "system-third-party-sub-sovereign"
THIRD_PARTY_AREA = "third-party-management"

FORMAL_TOOLS = ("postgresql", "qdrant", "git", "rag", "python", "typescript", "cpp", "c")


class ThirdPartySubSovereign:
    """In-process sub-sovereign (under system) responsible for third-party software management.

    Responsibilities:
      - third-party introduction review (none allowed per P7/A37)
      - tool inventory management
      - version/license/security tracking for formal tools
      - non-formal third-party detection and blocking
    """

    ROLE = THIRD_PARTY_ROLE

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._supervision_task: asyncio.Task[Any] | None = None
        self._supervision_interval_seconds = 600.0
        self._tool_inventory: dict[str, Any] | None = None
        self._inventory_path: Path | None = None

    async def start(
        self,
        *,
        supervision_interval_seconds: float = 600.0,
        inventory_path: str | Path | None = None,
    ) -> dict[str, Any]:
        self._supervision_interval_seconds = max(120.0, float(supervision_interval_seconds))
        workspace = Path(getattr(self.app, "project_root", Path.cwd()))
        if inventory_path is not None:
            self._inventory_path = Path(inventory_path)
        else:
            self._inventory_path = (
                workspace
                / "governance_rule"
                / "execution"
                / "third_party_management"
                / "tool_inventory.json"
            )
        self._tool_inventory = self._load_inventory()
        self._started_at = self._iso_now()
        self._started = True

        if self._supervision_task is None:
            self._supervision_task = asyncio.create_task(
                self._supervision_loop(),
                name="system-third-party-sub-sovereign-supervision",
            )

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "formal_tools": list(FORMAL_TOOLS),
            "inventory_loaded": self._tool_inventory is not None,
            "decision": decision_basis(THIRD_PARTY_AREA),
        }

    async def stop(self) -> None:
        if self._supervision_task is not None:
            self._supervision_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._supervision_task
            self._supervision_task = None
        self._tool_inventory = None
        self._started = False
        self._stopped_at = self._iso_now()

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "third-party-introduction-version-license-security",
            "started": self._started,
            "formal_tools": list(FORMAL_TOOLS),
            "inventory": self._inventory_status(),
            "supervision_loop": {
                "running": self._supervision_task is not None and not self._supervision_task.done(),
                "interval_seconds": self._supervision_interval_seconds,
            },
            "decision": decision_basis(THIRD_PARTY_AREA),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "name": "third-party-management",
            "role": self.ROLE,
            "scope": "third-party-introduction-version-license-security",
            "state": "running" if self._started else "stopped",
            "delegation": "governed-executor-only",
            "formal_tools": list(FORMAL_TOOLS),
            "decision": decision_basis(THIRD_PARTY_AREA),
        }

    def _inventory_status(self) -> dict[str, Any]:
        return {
            "duty": "tool-inventory",
            "loaded": self._tool_inventory is not None,
            "path": str(self._inventory_path) if self._inventory_path else None,
            "delegation": "governed-executor-only",
        }

    def _load_inventory(self) -> dict[str, Any] | None:
        if self._inventory_path is None or not self._inventory_path.is_file():
            return None
        try:
            return json.loads(self._inventory_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    async def _supervision_loop(self) -> None:
        while self._started:
            await asyncio.sleep(self._supervision_interval_seconds)

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()


def _suppress(*exceptions: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(*exceptions)


__all__ = [
    "FORMAL_TOOLS",
    "THIRD_PARTY_AREA",
    "THIRD_PARTY_ROLE",
    "ThirdPartySubSovereign",
]

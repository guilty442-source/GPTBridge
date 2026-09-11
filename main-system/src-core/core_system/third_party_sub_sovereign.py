"""Third-Party Sovereign — owns third-party software governance.

Per the Governance Codex (A51 / P25 / E37), the Third-Party Sovereign is a
``specialized-decision-sovereign`` (A127/E125) that owns third-party software
introduction, version, license, and security management.  It ensures no
non-formal third-party software, packages, or external services are
introduced (P7 / A37 / A49).

Duties (codex sovereign definition — A128):
  * govern-inventory-and-version
  * govern-license-and-security
  * decide-third-party-updates

Powers:
  * approve-dependency-entry
  * decide-dependency-version
  * accept-dependency-update

Prohibitions:
  * direct-ungoverned-execution
  * cross-sovereign-duty-takeover
  * permission-self-authorization

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates the tool
inventory and DELEGATES the actual enforcement to governed executors; it never
holds an execution power itself.

The sovereign delegates version probing, update checking, and update
execution to the ThirdPartyManager service.  Update execution requires an
explicit governance approval token.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .codex_decision import decision_basis
from .sovereign_utils import _iso_now, _suppress
from governance_rule.execution.tool_runtime.sub_sovereign import (
    SYSTEM_THIRD_PARTY_MANAGER_AUTHORITY,
)
from .third_party_manager import (
    AUTO_UPDATABLE_TOOLS,
    ThirdPartyManager,
    ToolVersionInfo,
    UpdateCheckResult,
    UpdateExecutionResult,
)
from governance_rule.codex import GOVERNANCE_CODEX


_THIRD_PARTY_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "dependency-governance"),
    None,
)
if _THIRD_PARTY_SOVEREIGN is None:
    raise RuntimeError("third-party sovereign not found in Governance Codex")

THIRD_PARTY_SUB_SOVEREIGN_RESPONSIBILITIES = _THIRD_PARTY_SOVEREIGN.duties

FORMAL_TOOLS = ("postgresql", "qdrant", "git", "rag", "python", "typescript", "cpp", "c", "csharp", "sql")


class ThirdPartySubSovereign:
    """In-process sovereign responsible for third-party software management.

    Responsibilities (codex sovereign definition — A128):
      - govern-inventory-and-version (tool inventory, version probing)
      - govern-license-and-security (non-formal detection and blocking)
      - decide-third-party-updates (update detection and governed execution)
    """

    ROLE = _THIRD_PARTY_SOVEREIGN.id

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._supervision_task: asyncio.Task[Any] | None = None
        self._supervision_interval_seconds = 600.0
        self._tool_inventory: dict[str, Any] | None = None
        self._inventory_path: Path | None = None
        self._manager: ThirdPartyManager | None = None

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
        self._manager = ThirdPartyManager(
            self._inventory_path,
            token_authenticator=self._token_authenticator,
        )
        self._started_at = _iso_now()
        self._started = True

        # Perform an initial version probe on startup
        try:
            self._manager.probe_all_versions()
        except Exception:
            pass

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
            "auto_updatable_tools": sorted(AUTO_UPDATABLE_TOOLS),
            "decision": decision_basis(SYSTEM_THIRD_PARTY_MANAGER_AUTHORITY),
        }

    async def stop(self) -> None:
        if self._supervision_task is not None:
            self._supervision_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._supervision_task
            self._supervision_task = None
        self._manager = None
        self._tool_inventory = None
        self._started = False
        self._stopped_at = _iso_now()

    # ─── Delegated operations ──────────────────────────────────────────

    def probe_all_versions(self) -> dict[str, ToolVersionInfo]:
        """Probe actual installed versions of all inventory tools."""
        if self._manager is None:
            return {}
        return self._manager.probe_all_versions()

    def check_all_for_updates(self) -> dict[str, UpdateCheckResult]:
        """Check for available updates on all auto-updatable tools."""
        if self._manager is None:
            return {}
        return self._manager.check_all_for_updates()

    async def apply_approved_update(
        self, tool_id: str, *, approval_token: str | None = None
    ) -> UpdateExecutionResult:
        """Apply an approved third-party update (A128: decide-third-party-updates)."""
        if self._manager is None:
            return UpdateExecutionResult(
                tool_id=tool_id, error="manager not initialized"
            )
        return await self._manager.execute_update(tool_id, approval_token=approval_token)

    async def apply_approved_auto_updates(
        self, *, approval_token: str, only_available: bool = True
    ) -> dict[str, UpdateExecutionResult]:
        """Apply approved auto-updates (A128: decide-third-party-updates)."""
        if self._manager is None:
            return {}
        return await self._manager.execute_auto_updates(
            approval_token=approval_token, only_available=only_available
        )

    def get_manager_status(self) -> dict[str, Any]:
        """Return the manager's full status for observability."""
        if self._manager is None:
            return {"ok": False, "error": "manager not initialized"}
        return self._manager.get_status()

    # ─── Status ────────────────────────────────────────────────────────

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "dependency-governance",
            "started": self._started,
            "formal_tools": list(FORMAL_TOOLS),
            "auto_updatable_tools": sorted(AUTO_UPDATABLE_TOOLS),
            "inventory": self._inventory_status(),
            "manager": self.get_manager_status() if self._manager else None,
            "update_boundary": "third-party-sovereign-managed; execution-only",
            "supervision_loop": {
                "running": self._supervision_task is not None and not self._supervision_task.done(),
                "interval_seconds": self._supervision_interval_seconds,
            },
            "decision": decision_basis(SYSTEM_THIRD_PARTY_MANAGER_AUTHORITY),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "name": "third-party-management",
            "role": self.ROLE,
            "scope": "dependency-governance",
            "state": "running" if self._started else "stopped",
            "delegation": "governed-executor-only",
            "formal_tools": list(FORMAL_TOOLS),
            "auto_updatable_tools": sorted(AUTO_UPDATABLE_TOOLS),
            "decision": decision_basis(SYSTEM_THIRD_PARTY_MANAGER_AUTHORITY),
        }

    def _token_authenticator(self, token: str) -> Any:
        """Authenticate a third-party approval token through the governance
        authentication service (fail-closed: any unavailable or invalid state
        raises so the manager denies the update)."""
        governance = getattr(self.app, "governance", None)
        if governance is None:
            raise PermissionError("governance-unavailable")
        authentication = getattr(governance, "_authentication", None)
        if authentication is None:
            authentication = getattr(governance, "authentication", None)
        if authentication is None or not callable(
            getattr(authentication, "authenticate_token", None)
        ):
            raise PermissionError("governance-authentication-unavailable")
        return authentication.authenticate_token(token)

    def _inventory_status(self) -> dict[str, Any]:
        return {
            "duty": "govern-inventory-and-version",
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
        """Periodically probe versions and check for updates.

        The supervision loop performs:
        1. Version probing of all inventory tools (detect drift)
        2. Update checking for auto-updatable tools
        3. Reports results to the governance log

        It does NOT auto-execute updates — that requires explicit governance
        approval and an IPC command from the user.
        """
        while self._started:
            try:
                if self._manager is not None:
                    self._manager.probe_all_versions()
                    self._manager.check_all_for_updates()
            except Exception:
                pass  # Supervision must never crash the sub-sovereign.
            await asyncio.sleep(self._supervision_interval_seconds)

__all__ = [
    "FORMAL_TOOLS",
    "THIRD_PARTY_SUB_SOVEREIGN_RESPONSIBILITIES",
    "ThirdPartySubSovereign",
]

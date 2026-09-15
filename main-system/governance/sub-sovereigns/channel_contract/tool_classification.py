"""Channel Contract Sync Sub-Sovereign — Tool Classification and Auto-Start (A306/A322)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from core_system.sovereign_utils import _iso_now


# Fallback resident service IDs used when manifest scanning is unavailable.
_FALLBACK_RESIDENT_TOOL_IDS = ("shared-layer",)

# Idle timeout: a module with no active execution request for this long is
# automatically stopped to conserve resources.  A subsequent request will
# auto-start it again via ensure_tool_running().  Resident services
# (lifecycle.stoppable == false) are exempt from idle stopping.
IDLE_TIMEOUT_SECONDS = float(__import__("os").environ.get("GPTBRIDGE_MODULE_IDLE_TIMEOUT", "300"))
IDLE_MONITOR_INTERVAL_SECONDS = 60.0


class ToolClassificationMixin:
    """Resident/non-resident classification and auto-start logic."""

    _resident_tool_ids: set[str]
    _non_resident_tool_ids: set[str]
    _default_tool_startup: dict[str, dict[str, Any]]
    _default_tools_started: bool
    _toolbox: Any
    _tool_last_activity: dict[str, float]
    app: Any

    def _classify_tools_by_manifest(self) -> None:
        """Scan tool manifests and classify each tool as resident or non-resident."""
        project_root = Path(getattr(self.app, "project_root", Path.cwd()))
        self._resident_tool_ids.clear()
        self._non_resident_tool_ids.clear()

        manifest_dirs = [
            path.parent
            for path in project_root.glob("*/manifest.json")
        ]
        standalone_root = project_root / "Standalone tools"
        if standalone_root.is_dir():
            manifest_dirs.extend(
                path.parent
                for path in standalone_root.glob("*/manifest.json")
            )
        for tool_dir in manifest_dirs:
            manifest_path = tool_dir / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            tool_id = str(manifest.get("id") or tool_dir.name).strip()
            if not tool_id:
                continue
            lifecycle = manifest.get("lifecycle") or {}
            stoppable = lifecycle.get("stoppable")
            if stoppable is False:
                self._resident_tool_ids.add(tool_id)
            else:
                self._non_resident_tool_ids.add(tool_id)

        if not self._resident_tool_ids:
            self._resident_tool_ids = set(_FALLBACK_RESIDENT_TOOL_IDS)

    async def _start_governed_default_tools(self) -> None:
        """Auto-start only RESIDENT services (常駐服務) at system startup."""
        if self._default_tools_started:
            return
        self._default_tools_started = True

        toolbox = self._toolbox
        if toolbox is None:
            return

        self._classify_tools_by_manifest()

        if "governance_rule" in self._resident_tool_ids:
            self._resident_tool_ids.discard("governance_rule")
            self._default_tool_startup["governance_rule"] = {
                "ok": True,
                "runtime_mode": "in-process-authority",
                "error_code": "",
                "message": "Governance Authority already loaded",
                "resident": True,
            }

        permission = getattr(self.app, "permission_sovereign", None)

        results = await asyncio.gather(
            *(
                self._start_resident_tool(toolbox, permission, tid)
                for tid in sorted(self._resident_tool_ids)
            ),
            return_exceptions=False,
        )

        for tool_id, result in results:
            self._default_tool_startup[tool_id] = {
                "ok": result.get("ok") is True,
                "runtime_mode": str(result.get("runtime_mode") or ""),
                "error_code": str(result.get("error_code") or ""),
                "message": str(result.get("message") or ""),
                "resident": True,
            }
            if result.get("ok") is True:
                self._tool_last_activity[tool_id] = time.monotonic()
                self._idle_stopped_tools.discard(tool_id)

    async def _start_resident_tool(
        self, toolbox: Any, permission: Any, tool_id: str
    ) -> tuple[str, dict[str, Any]]:
        if permission is None or not permission.can_start_tool(tool_id):
            return tool_id, {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        try:
            result = await toolbox.start_tool(
                {
                    "tool_id": tool_id,
                    "request_id": f"resident-start-{tool_id}-{time.time_ns()}",
                    "background": True,
                }
            )
        except Exception as error:
            result = {
                "ok": False,
                "tool_id": tool_id,
                "error_code": type(error).__name__,
                "message": str(error),
            }
        return tool_id, result
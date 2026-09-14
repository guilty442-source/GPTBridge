"""Third-Party Manager — facade.

This module provides the ThirdPartyManager class.  Constants, data
structures, and helpers live in
:mod:`core_system.third_party_manager_types`.

Centralized version probing, update detection, and update execution
for third-party tools.  Owned by the Third-Party Sub-Sovereign and
operates under governance supervision (A63/A130).

Windows background subprocess no-window flag: CREATE_NO_WINDOW.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .third_party_manager_types import (
    AUTO_UPDATABLE_TOOLS,
    THIRD_PARTY_MANAGER_VERSION,
    THIRD_PARTY_UPDATE_APPROVAL_CAPABILITIES,
    _PROBE_COMMANDS,
    _UPDATE_COMMANDS,
    _background_subprocess_kwargs,
    _iso_now,
    _normalize_version,
    ToolVersionInfo,
    UpdateCheckResult,
    UpdateExecutionResult,
)
from .third_party_manager_updates import ThirdPartyUpdateMixin


class ThirdPartyManager(ThirdPartyUpdateMixin):
    """Centralized third-party software version management service."""

    VERSION = THIRD_PARTY_MANAGER_VERSION

    def __init__(
        self,
        inventory_path: str | Path,
        *,
        token_authenticator: Callable[[str], Any] | None = None,
    ) -> None:
        self._inventory_path = Path(inventory_path)
        self._token_authenticator = token_authenticator
        self._version_cache: dict[str, ToolVersionInfo] = {}
        self._update_cache: dict[str, UpdateCheckResult] = {}
        self._last_full_probe: float = 0.0
        self._last_full_update_check: float = 0.0

    @property
    def inventory_path(self) -> Path:
        return self._inventory_path

    def load_inventory(self) -> dict[str, Any] | None:
        if not self._inventory_path.is_file():
            return None
        try:
            return json.loads(self._inventory_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def get_recorded_version(self, tool_id: str) -> str:
        inventory = self.load_inventory()
        if not inventory:
            return ""
        for tool in inventory.get("tools", []):
            if str(tool.get("id", "")).strip() == tool_id:
                return str(tool.get("version", "")).strip()
        return ""

    def is_auto_updatable(self, tool_id: str) -> bool:
        return tool_id in AUTO_UPDATABLE_TOOLS

    # ─── Version Probing ───────────────────────────────────────────────

    def probe_all_versions(self) -> dict[str, ToolVersionInfo]:
        """Probe versions for all tools in the inventory."""
        inventory = self.load_inventory()
        if not inventory:
            return {}

        results: dict[str, ToolVersionInfo] = {}
        for tool in inventory.get("tools", []):
            tool_id = str(tool.get("id", "")).strip()
            if not tool_id:
                continue
            results[tool_id] = self.probe_version(tool_id)

        self._version_cache = results
        self._last_full_probe = time.time()
        return results

    def get_cached_versions(self) -> dict[str, ToolVersionInfo]:
        return dict(self._version_cache)

    # ─── Update Checking ───────────────────────────────────────────────

    def check_for_updates(self, tool_id: str) -> UpdateCheckResult:
        """Check if an update is available for a single tool."""
        result = UpdateCheckResult(
            tool_id=tool_id,
            auto_updatable=self.is_auto_updatable(tool_id),
            last_checked_at=_iso_now(),
        )

        version_info = self._version_cache.get(tool_id)
        if version_info is None or not version_info.detected:
            version_info = self.probe_version(tool_id)
        result.current_version = version_info.detected_version

        if not result.current_version:
            result.error = "cannot determine current version"
            self._update_cache[tool_id] = result
            return result

        latest = self._fetch_latest_version(tool_id)
        if latest:
            result.latest_version = latest
            result.update_available = (
                _normalize_version(latest)
                != _normalize_version(result.current_version)
            )
        else:
            result.error = "could not determine latest version"

        self._update_cache[tool_id] = result
        return result

    def check_all_for_updates(self) -> dict[str, UpdateCheckResult]:
        """Check for updates for all auto-updatable tools."""
        results: dict[str, UpdateCheckResult] = {}
        for tool_id in AUTO_UPDATABLE_TOOLS:
            results[tool_id] = self.check_for_updates(tool_id)
        self._update_cache = results
        self._last_full_update_check = time.time()
        return results

    def _fetch_latest_version(self, tool_id: str) -> str:
        """Fetch the latest available version for a tool."""
        try:
            if tool_id == "uv":
                return self._fetch_latest_uv()
            if tool_id == "npm":
                return self._fetch_latest_npm()
            if tool_id == "ollama":
                return self._fetch_latest_ollama()
            if tool_id == "electron":
                return self._fetch_latest_electron()
        except Exception:
            return ""
        return ""

    def _fetch_latest_uv(self) -> str:
        try:
            subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                **_background_subprocess_kwargs(),
            )
            return ""
        except Exception:
            return ""

    def _fetch_latest_npm(self) -> str:
        try:
            result = subprocess.run(
                ["npm.cmd", "view", "npm", "version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                **_background_subprocess_kwargs(),
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _fetch_latest_ollama(self) -> str:
        return ""

    def _fetch_latest_electron(self) -> str:
        try:
            result = subprocess.run(
                ["npm.cmd", "view", "electron", "version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                **_background_subprocess_kwargs(),
            )
            return result.stdout.strip()
        except Exception:
            return ""

    # ─── Update Execution ──────────────────────────────────────────────

    def _verify_approval_token(
        self, approval_token: str | None
    ) -> tuple[bool, str]:
        """Authenticate a governed approval token (fail-closed)."""
        if not approval_token:
            return False, "approval token required"
        if self._token_authenticator is None:
            return False, "governance-authentication-unavailable"
        try:
            claims = self._token_authenticator(approval_token)
        except (PermissionError, ValueError) as exc:
            return False, f"permission-denied: {exc}"
        capability = getattr(claims, "capability", "")
        if capability not in THIRD_PARTY_UPDATE_APPROVAL_CAPABILITIES:
            return False, "capability-mismatch"
        return True, ""


    # ─── Status ────────────────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Return full status for observability and IPC."""
        versions = {
            tid: info.as_dict()
            for tid, info in self._version_cache.items()
        }
        updates = {
            tid: info.as_dict()
            for tid, info in self._update_cache.items()
        }
        return {
            "version": THIRD_PARTY_MANAGER_VERSION,
            "inventory_path": str(self._inventory_path),
            "auto_updatable_tools": sorted(AUTO_UPDATABLE_TOOLS),
            "last_full_probe_at": (
                datetime.fromtimestamp(self._last_full_probe, tz=timezone.utc).isoformat()
                if self._last_full_probe
                else None
            ),
            "last_full_update_check_at": (
                datetime.fromtimestamp(
                    self._last_full_update_check, tz=timezone.utc
                ).isoformat()
                if self._last_full_update_check
                else None
            ),
            "versions": versions,
            "update_checks": updates,
        }


__all__ = [
    "AUTO_UPDATABLE_TOOLS",
    "THIRD_PARTY_MANAGER_VERSION",
    "THIRD_PARTY_UPDATE_APPROVAL_CAPABILITIES",
    "ThirdPartyManager",
    "ToolVersionInfo",
    "UpdateCheckResult",
    "UpdateExecutionResult",
    "_normalize_version",
]

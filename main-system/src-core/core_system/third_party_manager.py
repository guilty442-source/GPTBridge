"""Third-Party Manager — centralized version probing, update detection, and update execution.

This service is owned by the Third-Party Sub-Sovereign and operates under
governance supervision.  It NEVER holds execution power itself — all update
actions are delegated to governed executors and require explicit governance
approval before execution.

Architecture:

  ThirdPartySubSovereign (governance)
    └─ ThirdPartyManager (service)
        ├─ VersionProbe     — detect actual installed versions
        ├─ UpdateChecker    — compare installed vs latest available
        ├─ UpdateExecutor   — execute governed updates
        └─ InventoryStore   — read/write tool_inventory.json

Update policy:

  Auto-updatable (safe, self-contained):
    - uv          → uv self update
    - npm         → npm install -g npm@latest
    - ollama      → ollama update (Windows: download installer)
    - electron    → npm update electron (local node_modules)

  Manual-only (system installers, admin required):
    - git, python, node, postgresql, msvc, windows-sdk, qdrant

  For manual-only tools, the manager detects version drift and reports
  it, but does NOT execute updates — that requires user action.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .versioning import component_version

THIRD_PARTY_MANAGER_VERSION = component_version("third-party-manager")

# Tools that can be auto-updated by the manager (safe, self-contained).
AUTO_UPDATABLE_TOOLS: frozenset[str] = frozenset({"uv", "npm", "ollama", "electron"})

# Capabilities that prove a governed approval token before any third-party
# update may execute.  Mirrors the governed hot-update approval gate so only
# tokens actually issued (and authenticated) by the governance service pass.
THIRD_PARTY_UPDATE_APPROVAL_CAPABILITIES: frozenset[str] = frozenset(
    {"hot-update", "hot-reload"}
)

# Probe commands: tool_id → (command, args, version_regex)
# The regex must have exactly one capturing group for the version string.
_PROBE_COMMANDS: dict[str, tuple[str, list[str], str]] = {
    "git": ("git", ["--version"], r"git version (\S+)"),
    "python": ("python", ["--version"], r"Python (\S+)"),
    "node": ("node", ["--version"], r"v(\S+)"),
    "npm": ("npm.cmd", ["--version"], r"(\S+)"),
    "uv": ("uv", ["--version"], r"uv (\S+)"),
    "ollama": ("ollama", ["--version"], r"version\s+(?:is\s+)?(\S+)"),
    "qdrant": ("qdrant", ["--version"], r"qdrant (?:version )?(\S+)"),
    "postgresql": ("psql", ["--version"], r"psql \(PostgreSQL\) (\S+)"),
}

# Update commands: tool_id → (command, args)
_UPDATE_COMMANDS: dict[str, tuple[str, list[str]]] = {
    "uv": ("uv", ["self", "update"]),
    "npm": ("npm.cmd", ["install", "-g", "npm@latest"]),
    "ollama": ("ollama", ["update"]),
}


def _background_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ToolVersionInfo:
    """Detected version information for a single tool."""

    tool_id: str
    recorded_version: str = ""
    detected_version: str = ""
    detected: bool = False
    path: str = ""
    last_probed_at: str = ""
    error: str = ""

    @property
    def version_matches(self) -> bool:
        if not self.detected or not self.recorded_version:
            return False
        return _normalize_version(self.detected_version) == _normalize_version(
            self.recorded_version
        )

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if not self.detected:
            return "missing"
        if self.recorded_version and not self.version_matches:
            return "version-drift"
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "recorded_version": self.recorded_version,
            "detected_version": self.detected_version,
            "detected": self.detected,
            "path": self.path,
            "last_probed_at": self.last_probed_at,
            "error": self.error,
            "status": self.status,
            "version_matches": self.version_matches,
        }


@dataclass
class UpdateCheckResult:
    """Result of checking for available updates."""

    tool_id: str
    auto_updatable: bool = False
    current_version: str = ""
    latest_version: str = ""
    update_available: bool = False
    last_checked_at: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "auto_updatable": self.auto_updatable,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "last_checked_at": self.last_checked_at,
            "error": self.error,
        }


@dataclass
class UpdateExecutionResult:
    """Result of executing an update."""

    tool_id: str
    ok: bool = False
    before_version: str = ""
    after_version: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    executed_at: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "ok": self.ok,
            "before_version": self.before_version,
            "after_version": self.after_version,
            "exit_code": self.exit_code,
            "executed_at": self.executed_at,
            "error": self.error,
            "stdout_bytes": len(self.stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(self.stderr.encode("utf-8", errors="replace")),
        }


def _normalize_version(version: str) -> str:
    """Normalize a version string for comparison (strip v prefix, build suffixes)."""
    v = version.strip()
    # Strip leading 'v' or 'V' (case-insensitive)
    if len(v) > 1 and v[0] in ("v", "V") and v[1].isdigit():
        v = v[1:]
    # Strip build suffixes like .windows.1
    v = re.split(r"[+\s]", v, maxsplit=1)[0]
    return v.casefold()


class ThirdPartyManager:
    """Centralized third-party software version management service.

    This service is safe to call from the governance sub-sovereign and from
    IPC command handlers.  All operations are read-only unless explicitly
    executing an update (which requires governance approval).
    """

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

    def probe_version(self, tool_id: str) -> ToolVersionInfo:
        """Probe the actual installed version of a single tool."""
        recorded = self.get_recorded_version(tool_id)
        info = ToolVersionInfo(
            tool_id=tool_id,
            recorded_version=recorded,
            last_probed_at=_iso_now(),
        )

        probe_spec = _PROBE_COMMANDS.get(tool_id)
        if probe_spec is None:
            info.error = f"no probe command for tool '{tool_id}'"
            self._version_cache[tool_id] = info
            return info

        command, args, version_regex = probe_spec
        executable = shutil.which(command)
        if executable is None:
            info.error = f"executable '{command}' not found in PATH"
            self._version_cache[tool_id] = info
            return info

        info.path = executable
        try:
            if tool_id == "git":
                from governance_rule.execution.git_tiers.git_repository import (
                    GitRepository,
                )

                result = GitRepository(Path.cwd()).run(
                    args,
                    actor="system-third-party-sub-sovereign/version-probe",
                )
            else:
                result = subprocess.run(
                    [command, *args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    **_background_subprocess_kwargs(),
                )
            output = (result.stdout or "") + (result.stderr or "")
            match = re.search(version_regex, output)
            if match:
                info.detected_version = match.group(1).strip()
                info.detected = True
            else:
                info.error = f"version pattern not found in output"
        except subprocess.TimeoutExpired:
            info.error = "probe timed out"
        except OSError as exc:
            info.error = f"probe failed: {exc}"

        self._version_cache[tool_id] = info
        return info

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

        # Get current version from cache or probe
        version_info = self._version_cache.get(tool_id)
        if version_info is None or not version_info.detected:
            version_info = self.probe_version(tool_id)
        result.current_version = version_info.detected_version

        if not result.current_version:
            result.error = "cannot determine current version"
            self._update_cache[tool_id] = result
            return result

        # For auto-updatable tools, check latest available version
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
            result = subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                **_background_subprocess_kwargs(),
            )
            # uv --version shows the current version; for latest we'd need
            # to check the registry.  Use uv self update --dry-run if available.
            # Fallback: compare with current (no update info available offline)
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
        # Ollama doesn't have a registry query; version check is done
        # by running `ollama update` which handles it internally.
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
        """Authenticate a governed approval token (fail-closed).

        Returns (authorized, reason).  Any missing authenticator, missing
        token, unauthenticated token, or capability mismatch denies the
        update before any command can run.
        """
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

    async def execute_update(
        self,
        tool_id: str,
        *,
        approval_token: str | None = None,
    ) -> UpdateExecutionResult:
        """Execute an update for a single tool.

        This is a governed action — it requires an authenticated approval
        token from the governance layer.  Only auto-updatable tools can be
        updated here.
        """
        result = UpdateExecutionResult(
            tool_id=tool_id,
            executed_at=_iso_now(),
        )

        if not self.is_auto_updatable(tool_id):
            result.error = (
                f"tool '{tool_id}' is not auto-updatable; "
                "manual update required (system installer)"
            )
            return result

        authorized, auth_message = self._verify_approval_token(approval_token)
        if not authorized:
            result.error = auth_message
            return result

        # Record before-version
        before = self.probe_version(tool_id)
        result.before_version = before.detected_version

        update_spec = _UPDATE_COMMANDS.get(tool_id)
        if update_spec is None:
            result.error = f"no update command for tool '{tool_id}'"
            return result

        command, args = update_spec
        executable = shutil.which(command)
        if executable is None:
            result.error = f"executable '{command}' not found in PATH"
            return result

        try:
            proc = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_background_subprocess_kwargs(),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
            result.stdout = stdout_bytes.decode("utf-8", errors="replace")
            result.stderr = stderr_bytes.decode("utf-8", errors="replace")
            result.exit_code = proc.returncode
            result.ok = proc.returncode == 0
        except asyncio.TimeoutExpired:
            result.error = "update timed out (120s)"
        except OSError as exc:
            result.error = f"update failed: {exc}"

        # Record after-version
        after = self.probe_version(tool_id)
        result.after_version = after.detected_version

        # Update the version cache
        self._version_cache[tool_id] = after

        return result

    async def execute_auto_updates(
        self,
        *,
        approval_token: str,
        only_available: bool = True,
    ) -> dict[str, UpdateExecutionResult]:
        """Execute updates for all auto-updatable tools.

        If only_available is True, only update tools where an update is
        confirmed available (requires a prior check_for_updates call).
        """
        authorized, auth_message = self._verify_approval_token(approval_token)
        if not authorized:
            raise PermissionError(f"permission-denied: {auth_message}")
        results: dict[str, UpdateExecutionResult] = {}
        for tool_id in AUTO_UPDATABLE_TOOLS:
            if only_available:
                check = self._update_cache.get(tool_id)
                if check is None or not check.update_available:
                    continue
            result = await self.execute_update(tool_id, approval_token=approval_token)
            results[tool_id] = result
        return results

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
]

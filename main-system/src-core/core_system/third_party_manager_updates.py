"""Third-party update execution mixin (A185 split).

Contains the probe_version, execute_update, and execute_auto_updates
methods extracted from ThirdPartyManager.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .third_party_manager_types import (
    AUTO_UPDATABLE_TOOLS,
    _PROBE_COMMANDS,
    _UPDATE_COMMANDS,
    _background_subprocess_kwargs,
    _iso_now,
    ToolVersionInfo,
    UpdateExecutionResult,
)


class ThirdPartyUpdateMixin:
    """Third-party version probing and update execution."""

    _version_cache: dict[str, ToolVersionInfo]
    _update_cache: dict[str, Any]
    _inventory_path: Path

    def get_recorded_version(self, tool_id: str) -> str | None:
        raise NotImplementedError

    def is_auto_updatable(self, tool_id: str) -> bool:
        raise NotImplementedError

    def _verify_approval_token(self, token: str | None) -> tuple[bool, str]:
        raise NotImplementedError

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
                    actor="dependency-sync-sub-sovereign/version-probe",
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

    async def execute_update(
        self,
        tool_id: str,
        *,
        approval_token: str | None = None,
    ) -> UpdateExecutionResult:
        """Execute an update for a single tool (governed action)."""
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

        after = self.probe_version(tool_id)
        result.after_version = after.detected_version
        self._version_cache[tool_id] = after
        return result

    async def execute_auto_updates(
        self,
        *,
        approval_token: str,
        only_available: bool = True,
    ) -> dict[str, UpdateExecutionResult]:
        """Execute updates for all auto-updatable tools."""
        from core_system.auto_action_policy import (
            automatic_update_execution_allowed,
            record_pending_action,
        )

        if not automatic_update_execution_allowed():
            try:
                from core_system.auto_action_policy import (
                    CONFIRMATION_TTL_SECONDS,
                )

                root = None
                for parent in self._inventory_path.resolve().parents:
                    if (parent / "main-system").is_dir():
                        root = parent
                        break
                if root is not None:
                    expires_at = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=CONFIRMATION_TTL_SECONDS)
                    ).isoformat()
                    record_pending_action(
                        root,
                        kind="update",
                        summary="third-party tool updates",
                        detail={
                            "tools": sorted(AUTO_UPDATABLE_TOOLS),
                            "only_available": only_available,
                        },
                        action_id="update-third-party-auto",
                        binding={
                            "update_id": "update-third-party-auto",
                            "scope": ", ".join(sorted(AUTO_UPDATABLE_TOOLS)),
                            "target": "auto-updatable third-party tools",
                            "proposed_method": "package-manager update",
                            "risk": "third-party version change",
                            "rollback": "reinstall previous version via package manager",
                            "expires_at": expires_at,
                        },
                    )
            except Exception:
                pass
            return {}
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


__all__ = ["ThirdPartyUpdateMixin"]

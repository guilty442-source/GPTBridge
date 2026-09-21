"""Tool environment construction mixin (A185 split).

Contains the _tool_environment method and companion cache resolution
helpers extracted from EnvironmentMixin.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from shared_layer.observability.tracing import get_correlation_context

from .toolbox_constants import (
    _MANAGED_BACKEND_TOOL_ID_ENV,
    _MANAGED_BACKEND_WORKSPACE_ID_ENV,
    _MANAGED_BACKEND_CODENAME_ENV,
    _TOOL_ENVIRONMENT_ALLOWLIST,
    _TOOL_GOVERNANCE_BOOTSTRAP_ENV,
    _is_declarable_tool_environment_key,
)


class EnvironmentConstructionMixin:
    """Tool environment construction and companion cache resolution."""

    tools_dir: Path
    project_root: Path
    governance: Any
    permission_sovereign: Any

    def _tool_environment(
        self,
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any] | None = None,
        *,
        start_hidden: bool = False,
        governance_tool_id: str | None = None,
    ) -> dict[str, str]:
        environment = manifest.get("environment") if isinstance(manifest, dict) else None
        declared_keys = environment.get("allow", []) if isinstance(environment, dict) else []
        if not isinstance(declared_keys, list):
            declared_keys = []
        tool_keys = {
            str(key).strip().upper()
            for key in declared_keys
            if _is_declarable_tool_environment_key(key)
        }
        allowed_keys = _TOOL_ENVIRONMENT_ALLOWLIST | tool_keys
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in allowed_keys
        }
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["PYTHONNOUSERSITE"] = "1"
        child_env["PYTHONUNBUFFERED"] = "1"
        isolated_tool_root = tool_dir.resolve()
        isolated_data_root = isolated_tool_root / "runtime"
        cache_root = isolated_data_root / "cache"
        host_owner = str(manifest.get("host_tool_id") or "").strip()
        if host_owner:
            try:
                host_dir = self._companion_host_dir(manifest, tool_dir)
                if host_dir is not None:
                    cache_root = self._validated_tool_path(
                        host_dir,
                        host_dir.joinpath(
                            *self._companion_cache_parts(host_dir, tool_id)
                        ),
                        label="Companion tool cache storage",
                    )
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        else:
            cache_root = (isolated_tool_root / "runtime" / "cache" / "companions" / tool_id).resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        # A533/A534: temporary storage is owned by the main-system internal
        # cleanup service; the retired global-cleaner is never addressed.
        temp_owner_root = self.project_root / "main-system"
        isolated_temp_root = self._validated_tool_path(
            temp_owner_root,
            temp_owner_root / "runtime" / "temp" / "tools" / tool_id,
            label="Tool temporary storage",
        )
        isolated_temp_root.mkdir(parents=True, exist_ok=True)
        isolated_temp_root = self._validated_tool_path(
            temp_owner_root,
            isolated_temp_root,
            label="Tool temporary storage",
        )
        child_env["GPTBRIDGE_PROJECT_ROOT"] = str(isolated_tool_root)
        child_env["GPTBRIDGE_STANDALONE_TOOL_ID"] = tool_id
        child_env["GPTBRIDGE_TOOL_ID"] = tool_id
        child_env["GPTBRIDGE_TOOL_DIR"] = str(isolated_tool_root)
        child_env["GPTBRIDGE_TOOL_DATA_ROOT"] = str(isolated_data_root)
        child_env["GPTBRIDGE_TOOL_SETTINGS_ROOT"] = str(
            isolated_data_root / "settings"
        )
        child_env["GPTBRIDGE_TOOL_DATABASE_ROOT"] = str(
            isolated_data_root / "state"
        )
        child_env["GPTBRIDGE_TOOL_CACHE_ROOT"] = str(cache_root)
        child_env["GPTBRIDGE_TOOL_TEMP_ROOT"] = str(isolated_temp_root)
        child_env["TEMP"] = str(isolated_temp_root)
        child_env["TMP"] = str(isolated_temp_root)
        child_env["TMPDIR"] = str(isolated_temp_root)
        if self.governance is None:
            raise PermissionError("PERMISSION_DENIED")
        owner = self._runtime_owner_tool_id(tool_id, manifest)
        channel_bound = self._governed_runtime_tool_id(owner)
        if (
            governance_tool_id is not None
            and str(governance_tool_id).strip() != channel_bound
        ):
            raise PermissionError("PERMISSION_DENIED")
        child_env[_TOOL_GOVERNANCE_BOOTSTRAP_ENV] = (
            self.permission_sovereign.create_tool_governance_bootstrap(
                channel_bound
            )
        )
        child_env["GPTBRIDGE_GOVERNANCE_PROJECT_ROOT"] = str(
            self.project_root.resolve()
        )
        child_env.pop("GPTBRIDGE_MANAGED_STORAGE_ROOT", None)
        child_env.pop("GPTBRIDGE_SYSTEM_RESCUE_STORAGE_AUTHORITY", None)
        if start_hidden:
            child_env["GPTBRIDGE_START_HIDDEN"] = "1"
        else:
            child_env.pop("GPTBRIDGE_START_HIDDEN", None)
        bindings = (
            environment.get("bindings", {})
            if isinstance(environment, dict)
            else {}
        )
        if isinstance(bindings, dict):
            for raw_key, source in bindings.items():
                key = str(raw_key).strip().upper()
                if not _is_declarable_tool_environment_key(key):
                    continue
                child_env.pop(key, None)
                if source == "project_root":
                    continue
                elif source == "tool_root":
                    child_env[key] = str(tool_dir.resolve())
        managed_keys = (
            _MANAGED_BACKEND_TOOL_ID_ENV,
            _MANAGED_BACKEND_WORKSPACE_ID_ENV,
            _MANAGED_BACKEND_CODENAME_ENV,
        )
        for key in managed_keys:
            child_env.pop(key, None)

        # Inject trace context into child process environment
        trace_ctx = get_correlation_context()
        if trace_ctx.get("correlation_id"):
            child_env["GPTBRIDGE_TRACE_ID"] = trace_ctx.get("trace_id", "")
            child_env["GPTBRIDGE_SPAN_ID"] = trace_ctx.get("span_id", "")
            child_env["GPTBRIDGE_PARENT_ID"] = trace_ctx.get("parent_id", "")
            child_env["GPTBRIDGE_CORRELATION_ID"] = trace_ctx.get("correlation_id", "")
            # Inject baggage as JSON
            if trace_ctx.get("baggage"):
                child_env["GPTBRIDGE_BAGGAGE"] = json.dumps(trace_ctx.get("baggage", {}))

        return child_env

    @staticmethod
    def _read_tool_manifest(tool_dir: Path) -> Dict[str, Any]:
        try:
            data = json.loads(
                (tool_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _companion_host_dir(
        self, manifest: Dict[str, Any], tool_dir: Path
    ) -> Path | None:
        """Resolve the declaring top-level host directory of a companion tool."""
        physical_root = str(manifest.get("physical_owner_root") or "").strip()
        parts = [
            part
            for part in physical_root.replace("\\", "/").split("/")
            if part and part not in {".", ".."}
        ]
        host_name = parts[0] if parts else ""
        if not host_name:
            try:
                host_name = tool_dir.resolve().relative_to(
                    self.tools_dir.resolve()
                ).parts[0]
            except (IndexError, ValueError):
                return None
        host_dir = (self.tools_dir / host_name).resolve()
        try:
            host_dir.relative_to(self.tools_dir.resolve())
        except ValueError:
            return None
        if not (host_dir / "manifest.json").is_file():
            return None
        return host_dir

    def _companion_cache_parts(
        self, host_dir: Path, tool_id: str
    ) -> list[str]:
        """Return the host-relative cache path parts for a companion tool."""
        host_manifest = self._read_tool_manifest(host_dir)
        host_id = str(host_manifest.get("id") or host_dir.name).strip()
        raw_storage = str(host_manifest.get("cache_storage") or "").strip()
        parts = [
            part
            for part in raw_storage.replace("\\", "/").split("/")
            if part and part not in {".", ".."}
        ]
        if parts and parts[0] in {host_id, host_dir.name}:
            parts = parts[1:]
        if not parts:
            parts = ["runtime", "cache"]
        return [*parts, "companions", tool_id]


__all__ = ["EnvironmentConstructionMixin"]

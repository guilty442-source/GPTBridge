"""Environment, argument validation, and runtime-mode helpers for ToolboxService."""
from __future__ import annotations

import hashlib
import os
import secrets
import socket
import uuid
from pathlib import Path
from typing import Any, Dict

from .tool_path_resolver import ToolPathResolver
from .toolbox_constants import (
    MAX_TOOL_ARGUMENTS,
    MAX_TOOL_ARGUMENT_BYTES,
    MAX_TOOL_REQUEST_ID_LENGTH,
    _MANAGED_BACKEND_TOOL_ID_ENV,
    _MANAGED_BACKEND_WORKSPACE_ID_ENV,
    _MANAGED_BACKEND_VERSION_ENV,
    _REQUIRED_TOOL_DISPLAY_VERSION,
    _REQUIRED_TOOL_VERSION,
    _TOOL_ENVIRONMENT_ALLOWLIST,
    _TOOL_GOVERNANCE_BOOTSTRAP_ENV,
    _is_declarable_tool_environment_key,
)


class EnvironmentMixin:
    """Environment construction, argument validation, and runtime-mode helpers."""

    # ------------------------------------------------------------------
    # Loopback / workspace helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _allocate_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _workspace_instance_id(self) -> str:
        normalized = os.path.normcase(str(self.project_root.resolve())).replace(
            "\\", "/"
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

    # ------------------------------------------------------------------
    # Source-runtime environment
    # ------------------------------------------------------------------

    def _source_runtime_environment(
        self,
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
    ) -> dict[str, str]:
        child_env = self._tool_environment(
            tool_id,
            tool_dir,
            manifest,
            start_hidden=True,
        )
        child_env["GPTBRIDGE_IPC_PORT"] = str(self._allocate_loopback_port())
        child_env["GPTBRIDGE_IPC_SESSION_TOKEN"] = secrets.token_hex(32)
        child_env["GPTBRIDGE_SHUTDOWN_TOKEN"] = secrets.token_hex(32)
        return child_env

    # ------------------------------------------------------------------
    # Runtime-mode decision helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _source_launch_requested(
        manifest: Dict[str, Any],
        *,
        background: bool,
        requested_mode: str,
        executable_exists: bool,
    ) -> bool:
        if not ToolPathResolver.has_governed_source_runtime(manifest):
            return False
        if requested_mode == "source":
            return True
        if requested_mode == "executable":
            return False
        if ToolPathResolver.is_special_unpacked(manifest):
            return True
        launch = manifest.get("launch")
        primary = str(launch.get("primary") or "") if isinstance(launch, dict) else ""
        if background:
            return True
        if ToolPathResolver.is_dual_runtime(manifest):
            return not executable_exists
        if ToolPathResolver.has_governed_background_source(manifest):
            return not executable_exists
        return primary != "executable" or not executable_exists

    @staticmethod
    def _source_fallback_allowed(
        manifest: Dict[str, Any],
        requested_mode: str,
    ) -> bool:
        if not ToolPathResolver.has_governed_source_runtime(manifest):
            return False
        if ToolPathResolver.is_dual_runtime(manifest):
            return requested_mode != "source"
        if requested_mode == "executable":
            return False
        launch = manifest.get("launch")
        return (
            isinstance(launch, dict)
            and str(launch.get("fallback") or "").strip().casefold()
            == "governed-source-ui"
        )

    @staticmethod
    def _executable_fallback_allowed(
        manifest: Dict[str, Any],
        requested_mode: str,
        executable_exists: bool,
    ) -> bool:
        if requested_mode == "executable" or not executable_exists:
            return False
        if ToolPathResolver.is_dual_runtime(manifest):
            return True
        launch = manifest.get("launch")
        return bool(
            isinstance(launch, dict)
            and str(launch.get("fallback") or "").strip().casefold()
            == "executable"
        )

    @staticmethod
    def _tool_version_failure(
        tool_id: str,
        request_id: str,
        manifest: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        version = str(manifest.get("version") or "").strip()
        display_version = str(manifest.get("display_version") or "").strip()
        errors: list[str] = []
        if version != _REQUIRED_TOOL_VERSION:
            errors.append(
                f"tool version must be {_REQUIRED_TOOL_VERSION}; "
                f"found {version or 'missing'}"
            )
        if display_version and display_version != _REQUIRED_TOOL_DISPLAY_VERSION:
            errors.append(
                "tool display version must be "
                f"{_REQUIRED_TOOL_DISPLAY_VERSION}; found {display_version}"
            )
        if not errors:
            return None
        return {
            "ok": False,
            "tool_id": tool_id,
            "request_id": request_id,
            "error_code": "TOOL_VERSION_MISMATCH",
            "message": "; ".join(errors),
        }

    # ------------------------------------------------------------------
    # Tool environment construction
    # ------------------------------------------------------------------

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
        isolated_tool_root = tool_dir.resolve()
        isolated_data_root = isolated_tool_root / "runtime"
        cache_root = isolated_data_root / "cache"
        cache_owner_root = isolated_tool_root
        shared_cache_owner = str(
            (manifest.get("shared_cache_owner") or "")
            if isinstance(manifest, dict)
            else ""
        ).strip()
        if shared_cache_owner:
            permissions = (
                manifest.get("permissions") if isinstance(manifest, dict) else None
            )
            permission_owner = (
                str(permissions.get("business_permission_owner") or "").strip()
                if isinstance(permissions, dict)
                else ""
            )
            host_owner = str(manifest.get("host_tool_id") or "").strip()
            manifest_tool_id = str(manifest.get("id") or "").strip()
            if shared_cache_owner not in {
                permission_owner,
                host_owner,
                manifest_tool_id,
            }:
                raise ValueError("Shared cache owner is not the declared business owner")
            cache_owner_root = self._tool_directory_for_id(shared_cache_owner)
            cache_root = (
                cache_owner_root
                / "runtime"
                / "cache"
                / "companions"
                / tool_id
            )
            cache_root = self._validated_tool_path(
                cache_owner_root,
                cache_root,
                label="Shared tool cache storage",
            )
        cache_root.mkdir(parents=True, exist_ok=True)
        cleaner_root = self._validated_tool_directory(
            self.tools_dir / "global-cleaner"
        )
        temp_candidate = (
            cleaner_root / "runtime" / "temp" / "tools" / tool_id
        )
        isolated_temp_root = self._validated_tool_path(
            cleaner_root,
            temp_candidate,
            label="Tool temporary storage",
        )
        isolated_temp_root.mkdir(parents=True, exist_ok=True)
        isolated_temp_root = self._validated_tool_path(
            cleaner_root,
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
        bootstrap_tool_id = governance_tool_id or tool_id
        child_env[_TOOL_GOVERNANCE_BOOTSTRAP_ENV] = (
            self.permission_sovereign.create_tool_governance_bootstrap(
                bootstrap_tool_id
            )
        )
        child_env["GPTBRIDGE_GOVERNANCE_PROJECT_ROOT"] = str(
            self.project_root.resolve()
        )
        if tool_id == "global-cleaner":
            child_env["GPTBRIDGE_GLOBAL_CLEANER_TARGET_ROOT"] = str(
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
                # A binding is controlled by the manifest, never by an
                # ambient parent-process value with the same name.
                child_env.pop(key, None)
                if source == "project_root":
                    # Tool manifests contain business configuration only and
                    # cannot grant project-wide authority to themselves.
                    continue
                elif source == "tool_root":
                    child_env[key] = str(tool_dir.resolve())
        managed_keys = (
            _MANAGED_BACKEND_TOOL_ID_ENV,
            _MANAGED_BACKEND_WORKSPACE_ID_ENV,
            _MANAGED_BACKEND_VERSION_ENV,
        )
        for key in managed_keys:
            child_env.pop(key, None)
        return child_env

    # ------------------------------------------------------------------
    # Argument / request-id validation
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_arguments(payload: Dict[str, Any], tool_id: str = "") -> tuple[list[str] | None, Dict[str, Any] | None]:
        raw_args = payload.get("args", [])
        result_base = {"tool_id": tool_id} if tool_id else {}
        if not isinstance(raw_args, list):
            return None, {
                "ok": False,
                **result_base,
                "error_code": "INVALID_ARGUMENTS",
                "message": "args must be a list",
            }
        if len(raw_args) > MAX_TOOL_ARGUMENTS:
            return None, {
                "ok": False,
                **result_base,
                "error_code": "TOO_MANY_ARGUMENTS",
                "argument_count": len(raw_args),
                "max_arguments": MAX_TOOL_ARGUMENTS,
                "message": f"args exceeds the explicit limit of {MAX_TOOL_ARGUMENTS} items",
            }
        try:
            args = [str(item) for item in raw_args]
        except Exception as exc:
            return None, {
                "ok": False,
                **result_base,
                "error_code": "INVALID_ARGUMENTS",
                "message": f"args could not be converted to strings: {exc}",
            }
        argument_bytes = sum(len(arg.encode("utf-8")) for arg in args)
        if argument_bytes > MAX_TOOL_ARGUMENT_BYTES:
            return None, {
                "ok": False,
                **result_base,
                "error_code": "ARGUMENTS_TOO_LARGE",
                "argument_bytes": argument_bytes,
                "max_argument_bytes": MAX_TOOL_ARGUMENT_BYTES,
                "message": (
                    "encoded args exceeds the explicit limit of "
                    f"{MAX_TOOL_ARGUMENT_BYTES} bytes"
                ),
            }
        return args, None

    @staticmethod
    def _tool_request_id(payload: Dict[str, Any]) -> tuple[str | None, Dict[str, Any] | None]:
        raw_request_id = payload.get("request_id")
        request_id = str(raw_request_id).strip() if raw_request_id is not None else ""
        if not request_id:
            return uuid.uuid4().hex, None
        if len(request_id) > MAX_TOOL_REQUEST_ID_LENGTH or any(
            ord(character) < 32 for character in request_id
        ):
            return None, {
                "ok": False,
                "error_code": "INVALID_REQUEST_ID",
                "max_request_id_length": MAX_TOOL_REQUEST_ID_LENGTH,
                "message": (
                    "request_id must be a printable, non-empty string no longer than "
                    f"{MAX_TOOL_REQUEST_ID_LENGTH} characters"
                ),
            }
        return request_id, None

"""Environment, argument validation, and runtime-mode helpers for ToolboxService."""
from __future__ import annotations

import hashlib
import json
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
    _MANAGED_BACKEND_CODENAME_ENV,
    _TOOL_VERSION_PATTERN,
    _TOOL_ENVIRONMENT_ALLOWLIST,
    _TOOL_GOVERNANCE_BOOTSTRAP_ENV,
    _is_declarable_tool_environment_key,
)
from .toolbox_environment_construction import EnvironmentConstructionMixin


class EnvironmentMixin(EnvironmentConstructionMixin):
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
        governed_tool_id = self._governed_runtime_tool_id(tool_id, manifest)
        child_env = self._tool_environment(
            tool_id,
            tool_dir,
            manifest,
            start_hidden=True,
            governance_tool_id=governed_tool_id,
        )
        child_env["GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID"] = governed_tool_id
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
        requested = str(requested_mode).strip().casefold()
        if requested == "executable":
            return False
        if requested == "source":
            return True
        launch = manifest.get("launch") or {}
        if background and str(launch.get("background") or "").strip().casefold() in {
            "governed-source",
            "governed-source-ui",
            "governed-source-channel",
            "source",
        }:
            return True
        if ToolPathResolver.is_dual_runtime(manifest):
            return not executable_exists
        primary_is_source = str(launch.get("primary") or "").strip().casefold() in {
            "governed-source",
            "governed-source-ui",
            "governed-source-channel",
            "source",
        }
        if primary_is_source:
            return True
        return not executable_exists

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
        if not executable_exists:
            return False
        if not ToolPathResolver.is_dual_runtime(manifest):
            return False
        requested = str(requested_mode).strip().casefold()
        return requested in {"", "source"}

    @staticmethod
    def _tool_version_failure(
        tool_id: str,
        request_id: str,
        manifest: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        version = str(manifest.get("version") or "").strip()
        display_version = str(manifest.get("display_version") or "").strip()
        errors: list[str] = []
        if _TOOL_VERSION_PATTERN.fullmatch(version) is None:
            errors.append(f"tool version is invalid: {version or 'missing'}")
        expected_display = ".".join(version.split(".")[:2])
        if display_version and display_version != expected_display:
            errors.append(
                f"tool display version must match {expected_display}; found {display_version}"
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

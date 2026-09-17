"""Tool start validation and runtime resolution mixin.

Provides validation, authorization, manifest loading, runtime resolution,
and package verification phases for the tool start lifecycle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

from .toolbox_start_package_verification import PackageVerificationMixin


class StartValidationMixin(PackageVerificationMixin):
    """Validation and runtime resolution methods for tool start."""

    def _validate_start_request(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any] | None:
        """Validate the start request. Returns error dict or None if valid."""
        if not self._maintenance_ready:
            return self._maintenance_not_ready_result("start_tool")
        tool_id = str(payload.get("tool_id", "")).strip()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}
        try:
            self._authorize_tool_lifecycle(tool_id, "start")
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        self._force_closed_tool_ids.discard(tool_id)
        return None

    def _resolve_start_context(
        self, payload: Dict[str, Any], tool_id: str
    ) -> Dict[str, Any] | None:
        """Resolve request ID, arguments, tool dir, and manifest.
        Returns error dict or None with context in self._start_ctx."""
        request_id, request_error = self._tool_request_id(payload)
        if request_error is not None or request_id is None:
            return {"tool_id": tool_id, **(request_error or {})}
        args, argument_error = self._tool_arguments(payload, tool_id)
        if argument_error is not None or args is None:
            if argument_error is not None:
                argument_error["request_id"] = request_id
            return argument_error or {"ok": False, "tool_id": tool_id, "request_id": request_id}

        try:
            tool_dir = self._tool_directory_for_id(tool_id)
        except ValueError as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "INVALID_TOOL_PATH",
                "message": str(error),
            }
        manifest_path = tool_dir / "manifest.json"
        if not manifest_path.exists():
            self._forget_missing_tool(tool_id)
            return {**self._missing_tool_result(tool_id), "request_id": request_id}

        try:
            manifest, _cached_dir = self._load_manifest_cached(tool_id)
        except Exception as exc:
            return {"ok": False, "tool_id": tool_id, "message": f"Invalid tool manifest: {exc}"}

        if manifest.get("enabled", True) is False:
            return {"ok": False, "tool_id": tool_id, "message": "Tool is disabled"}

        self._start_ctx = {
            "request_id": request_id,
            "args": args,
            "tool_dir": tool_dir,
            "manifest": manifest,
        }
        return None

    async def _resolve_runtime_path(
        self, tool_id: str, request_id: str, manifest: dict, tool_dir: Path,
        background: bool, requested_mode: str, repair_attempted: bool,
        fallback_attempted: bool, payload: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        """Resolve executable/source runtime path. Returns error dict or None."""
        version_failure = self._tool_version_failure(tool_id, request_id, manifest)
        if version_failure is not None:
            await self.update_status(tool_id, "error")
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload, tool_id, tool_dir, manifest, version_failure,
                )
            return version_failure

        source_runtime = self._has_governed_source_runtime(manifest)
        source_entry: Path | None = None
        python_executable: Path | None = None
        try:
            executable_file = self._resolve_executable_file(manifest, tool_dir)
            if source_runtime:
                source_entry = self._resolve_special_unpacked_entry(manifest, tool_dir)
                python_executable = self._resolve_python_executable(manifest, tool_dir)
        except ValueError as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": (
                    "INVALID_SOURCE_RUNTIME" if source_runtime else "INVALID_EXECUTABLE_PATH"
                ),
                "message": str(error),
            }
        use_source_runtime = self._source_launch_requested(
            manifest, background=background, requested_mode=requested_mode,
            executable_exists=executable_file.exists(),
        )
        # Independent tools must launch from governed native source code.
        if not source_runtime:
            await self.update_status(tool_id, "stopped")
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "GOVERNED_SOURCE_RUNTIME_REQUIRED",
                "message": "Independent tools must launch from governed source code; EXE launch is disabled",
            }
        runtime_path = source_entry if use_source_runtime else executable_file
        runtime_mode = "governed-source" if use_source_runtime else "executable"

        self._start_ctx.update({
            "source_runtime": source_runtime,
            "source_entry": source_entry,
            "python_executable": python_executable,
            "executable_file": executable_file,
            "use_source_runtime": use_source_runtime,
            "runtime_path": runtime_path,
            "runtime_mode": runtime_mode,
        })
        return None

    def _package_failure_result(
        self, tool_id: str, request_id: str, package_check: dict, executable_file: Path
    ) -> dict:
        return {
            "ok": False, "tool_id": tool_id, "request_id": request_id,
            "error_code": "PACKAGE_UNVERIFIED",
            "package_error_code": str(package_check.get("error_code") or ""),
            "message": (
                f"{package_check.get('message', 'Package verification failed')}. "
                f"Run npm run package:tool -- {tool_id}"
            ),
            "executable_path": str(executable_file),
        }

    def _stale_package_result(self, tool_id: str, request_id: str, executable_file: Path) -> dict:
        return {
            "ok": False, "tool_id": tool_id, "request_id": request_id,
            "error_code": "STALE_TOOL_PACKAGE",
            "message": (
                "This tool's manifest version changed after its EXE was packaged. "
                f"Run npm run package:tool -- {tool_id}"
            ),
            "executable_path": str(executable_file),
        }

    async def _handle_package_failure(
        self, failure_result: dict, payload: dict, tool_id: str,
        tool_dir: Path, manifest: dict, fallback_attempted: bool,
        repair_attempted: bool, requested_mode: str,
    ) -> Dict[str, Any] | None:
        """Handle a package failure with fallback or repair. Returns error dict or None to continue."""
        if not fallback_attempted and self._source_fallback_allowed(manifest, requested_mode):
            fallback_payload = dict(payload)
            fallback_payload["runtime_mode"] = "source"
            fallback_payload["_source_fallback_attempted"] = True
            fallback_payload["_executable_fallback_attempted"] = True
            return await self.start_tool(fallback_payload)
        if not repair_attempted:
            return await self._retry_start_after_central_repair(
                payload, tool_id, tool_dir, manifest, failure_result,
            )
        if self._source_fallback_allowed(manifest, requested_mode):
            return None  # Signal: use source fallback
        return failure_result

    async def _check_contract_version(
        self, tool_id: str, request_id: str, executable_file: Path,
        package_metadata: dict, payload: dict, tool_dir: Path,
        manifest: dict, fallback_attempted: bool,
        repair_attempted: bool, requested_mode: str,
    ) -> Dict[str, Any] | None:
        """Check runtime contract compatibility. Returns error dict or None."""
        ctx = self._start_ctx
        use_source_runtime = ctx["use_source_runtime"]
        source_entry = ctx["source_entry"]

        try:
            contract = json.loads(
                (
                    self.project_root / "main-system" / "config"
                    / "tool-runtime-contract.json"
                ).read_text(encoding="utf-8")
            )
            current_contract = int(contract["contract_version"])
            minimum_contract = int(contract["minimum_supported_contract_version"])
            packaged_contract = int(
                package_metadata.get("runtime_contract_version") or 1
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            current_contract = 1
            minimum_contract = 1
            packaged_contract = int(
                package_metadata.get("runtime_contract_version") or 1
            )
        if (
            not use_source_runtime
            and not minimum_contract <= packaged_contract <= current_contract
        ):
            await self.update_status(tool_id, "error")
            failure_result = {
                "ok": False, "tool_id": tool_id, "request_id": request_id,
                "error_code": "INCOMPATIBLE_TOOL_RUNTIME",
                "message": (
                    "The packaged tool runtime contract is incompatible with "
                    "this GPTBridge version."
                ),
                "executable_path": str(executable_file),
            }
            if not fallback_attempted and self._source_fallback_allowed(manifest, requested_mode):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload, tool_id, tool_dir, manifest, failure_result,
                )
            if self._source_fallback_allowed(manifest, requested_mode):
                ctx["use_source_runtime"] = True
                ctx["runtime_path"] = source_entry
                ctx["runtime_mode"] = "governed-source"
            else:
                return failure_result
        return None

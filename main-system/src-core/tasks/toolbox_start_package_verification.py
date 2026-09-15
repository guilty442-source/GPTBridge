"""Tool start package verification mixin (A185 split).

Contains the _verify_package_and_contract method extracted from
StartValidationMixin.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from governance_rule.execution.integrity.package_integrity import (
    load_package_metadata,
    verify_packaged_app,
)


class PackageVerificationMixin:
    """Package integrity and runtime contract verification."""

    _start_ctx: dict[str, Any]

    async def update_status(self, tool_id: str, status: str) -> Dict[str, Any]:
        raise NotImplementedError

    def _source_fallback_allowed(self, manifest: dict, requested_mode: str) -> bool:
        raise NotImplementedError

    async def start_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def _retry_start_after_central_repair(
        self, payload: Dict[str, Any], tool_id: str, tool_dir: Path,
        manifest: dict, failure_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def _package_failure_result(
        self, tool_id: str, request_id: str, package_check: dict, executable_file: Path
    ) -> dict:
        raise NotImplementedError

    async def _handle_package_failure(
        self, failure_result: Dict[str, Any], payload: Dict[str, Any],
        tool_id: str, tool_dir: Path, manifest: dict,
        fallback_attempted: bool, repair_attempted: bool, requested_mode: str,
    ) -> Dict[str, Any] | None:
        raise NotImplementedError

    def _stale_package_result(
        self, tool_id: str, request_id: str, executable_file: Path
    ) -> dict:
        raise NotImplementedError

    async def _check_contract_version(
        self, tool_id: str, request_id: str, executable_file: Path,
        package_metadata: dict, payload: Dict[str, Any], tool_dir: Path,
        manifest: dict, fallback_attempted: bool,
        repair_attempted: bool, requested_mode: str,
    ) -> Dict[str, Any] | None:
        raise NotImplementedError

    async def _verify_package_and_contract(
        self, tool_id: str, request_id: str, manifest: dict,
        payload: Dict[str, Any], requested_mode: str,
        repair_attempted: bool, fallback_attempted: bool,
    ) -> Dict[str, Any] | None:
        """Verify package integrity and runtime contract. Returns error dict or None."""
        ctx = self._start_ctx
        use_source_runtime = ctx["use_source_runtime"]
        executable_file = ctx["executable_file"]
        source_entry = ctx["source_entry"]
        tool_dir = ctx["tool_dir"]

        if not use_source_runtime and not executable_file.exists():
            await self.update_status(tool_id, "stopped")
            failure_result = {
                "ok": False, "tool_id": tool_id, "request_id": request_id,
                "error_code": "EXECUTABLE_MISSING",
                "message": f"Standalone EXE not found. Run npm run package:tool -- {tool_id}",
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
            return failure_result

        package_check = (
            {"ok": True} if use_source_runtime
            else verify_packaged_app(executable_file.parent / "resources" / "app")
        )
        if not package_check.get("ok"):
            await self.update_status(tool_id, "error")
            failure_result = self._package_failure_result(
                tool_id, request_id, package_check, executable_file
            )
            result = await self._handle_package_failure(
                failure_result, payload, tool_id, tool_dir, manifest,
                fallback_attempted, repair_attempted, requested_mode,
            )
            if result is not None:
                return result
            # Source fallback succeeded — update context
            ctx["use_source_runtime"] = True
            ctx["runtime_path"] = source_entry
            ctx["runtime_mode"] = "governed-source"

        package_metadata = (
            {} if use_source_runtime
            else load_package_metadata(executable_file.parent / "resources" / "app")
        )
        source_version = str(manifest.get("version") or "").strip()
        packaged_version = str(
            package_metadata.get("tool_version") or source_version
        ).strip()
        if not use_source_runtime and (not source_version or packaged_version != source_version):
            await self.update_status(tool_id, "error")
            failure_result = self._stale_package_result(
                tool_id, request_id, executable_file
            )
            result = await self._handle_package_failure(
                failure_result, payload, tool_id, tool_dir, manifest,
                fallback_attempted, repair_attempted, requested_mode,
            )
            if result is not None:
                return result
            ctx["use_source_runtime"] = True
            ctx["runtime_path"] = source_entry
            ctx["runtime_mode"] = "governed-source"
            package_metadata = {}

        # Contract version check
        contract_result = await self._check_contract_version(
            tool_id, request_id, executable_file, package_metadata,
            payload, tool_dir, manifest, fallback_attempted,
            repair_attempted, requested_mode,
        )
        if contract_result is not None:
            return contract_result

        return None


__all__ = ["PackageVerificationMixin"]

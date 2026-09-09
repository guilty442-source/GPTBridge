"""Central repair, backup extraction, and retry-after-repair helpers."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict

from .central_repair import CentralRepairService


class RepairMixin:
    """Central automatic repair, backup extraction, and retry orchestration."""

    @property
    def central_repair(self) -> CentralRepairService:
        if self._central_repair is None:
            repair_data_root = self.project_root / "main-system" / "data" / "automatic-repair"
            repair_data_root.mkdir(parents=True, exist_ok=True)
            self._central_repair = CentralRepairService(
                self.project_root, repair_data_root
            )
        return self._central_repair

    async def _request_central_repair(
        self,
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
        failure: Dict[str, Any],
    ) -> dict[str, Any]:
        del tool_dir, manifest
        if tool_id in {"main-system"}:
            return {
                "triggered": False,
                "reason": "CENTRAL_REPAIR_OWNER_CANNOT_REPAIR_ITSELF",
            }
        failure_code = str(failure.get("error_code") or "TOOL_START_FAILED")[:128]
        try:
            from .package_rebuilder import ToolPackageRebuilder
            rebuilder = ToolPackageRebuilder(
                self.project_root,
                self.project_root / "main-system",
            )
            repair_result = await asyncio.to_thread(
                self.central_repair.repair_tool,
                tool_id,
                failure_code,
                package_rebuilder=rebuilder.rebuild,
            )
            extraction: dict[str, Any] | None = None
            extract_paths = repair_result.get("backup_extract_paths") or []
            if extract_paths and self.governance is not None:
                extraction = await self._request_backup_extraction(
                    tool_id, extract_paths
                )
            return {
                "triggered": True,
                "ok": repair_result.get("ok") is True,
                "authority": "main-system",
                "channel": "integrated-central-repair",
                "detail": repair_result,
                "backup_extraction": extraction,
            }
        except (OSError, ValueError, PermissionError) as error:
            return {
                "triggered": True,
                "ok": False,
                "error_code": "CENTRAL_AUTO_REPAIR_FAILED",
                "message": str(error),
            }

    async def _request_backup_extraction(
        self, target_tool_id: str, paths: list[str]
    ) -> dict[str, Any]:
        if not paths or not self.governance:
            return {"status": "skipped", "reason": "no-extraction-required"}
        started_here = False
        try:
            start_result = await self.start_tool(
                {
                    "tool_id": "global-cleaner",
                    "request_id": (
                        f"central-repair-extract-start-{time.time_ns()}"
                    ),
                    "background": True,
                    "runtime_mode": "source",
                    "_auto_repair_attempted": True,
                }
            )
            if start_result.get("ok") is not True:
                return {
                    "status": "service_start_failed",
                    "detail": start_result,
                }
            if "already running" not in str(
                start_result.get("message") or ""
            ).casefold():
                started_here = True
            extraction_args = [
                "--extract-managed-backup",
                "--backup-owner",
                target_tool_id,
            ]
            for path in paths:
                extraction_args.extend(("--backup-path", str(path)))
            extraction_args.append("--json")
            extraction_request_id = (
                f"repair-extract-{target_tool_id}-{time.time_ns()}"
            )
            queued = await self.request_tool_execution(
                {
                    "tool_id": "global-cleaner",
                    "request_id": extraction_request_id,
                    "_governed_command": "toolbox_request_tool_execution",
                    "args": extraction_args,
                }
            )
            if queued.get("ok") is not True:
                return {
                    "status": "queue_failed",
                    "detail": queued,
                }
            extraction = None
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                extraction = await asyncio.to_thread(
                    self.permission_sovereign.tool_execution_response,
                    "global-cleaner",
                    extraction_request_id,
                )
                if extraction and extraction.get("status") in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    break
                await asyncio.sleep(0.5)
            return extraction or {
                "status": "timed_out",
                "request_id": extraction_request_id,
            }
        except (OSError, ValueError, PermissionError) as error:
            return {"status": "failed", "message": str(error)}
        finally:
            if started_here:
                await self.force_close_tool(
                    {
                        "tool_id": "global-cleaner",
                        "request_id": (
                            f"central-repair-extract-stop-{time.time_ns()}"
                        ),
                    }
                )

    async def _retry_start_after_central_repair(
        self,
        payload: Dict[str, Any],
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
        failure: Dict[str, Any],
    ) -> Dict[str, Any]:
        repair_result = await self._request_central_repair(
            tool_id,
            tool_dir,
            manifest,
            failure,
        )
        central_result = (
            repair_result.get("detail")
            if isinstance(repair_result.get("detail"), dict)
            else {}
        )
        package_repair = (
            central_result.get("package_repair")
            if isinstance(central_result, dict)
            else None
        )
        if not isinstance(package_repair, dict):
            package_repair = {
                "triggered": False,
                "owner": "main-system",
                "reason": "PACKAGE_REBUILD_NOT_REQUIRED",
            }
        if repair_result.get("ok") is not True:
            central_message = (
                central_result.get("message")
                if isinstance(central_result, dict)
                else None
            )
            return {
                **failure,
                "ok": False,
                "auto_repair": repair_result,
                "package_repair": package_repair,
                "message": str(
                    central_message
                    or "Central automatic repair could not restore tool stability"
                ),
            }
        retry_payload = dict(payload)
        retry_payload["_auto_repair_attempted"] = True
        retry_result = await self.start_tool(retry_payload)
        retry_result["auto_repair"] = repair_result
        retry_result["package_repair"] = package_repair
        return retry_result

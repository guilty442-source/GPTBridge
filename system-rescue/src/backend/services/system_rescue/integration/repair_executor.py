from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from shared_layer.governed_runtime import GovernedCliExecutor


class CentralRepairExecutor:
    """Run repair locally, then request governed backup extraction when needed."""

    def __init__(self, tool_id: str, tool_root: Path) -> None:
        self.cli = GovernedCliExecutor(tool_id, tool_root)
        self.channel: Any | None = None

    async def __call__(
        self, command: str, payload: dict[str, Any], request_id: str
    ) -> tuple[str, dict[str, Any]]:
        event, result = await self.cli(command, payload, request_id)
        raw_args = payload.get("args", [])
        if not isinstance(raw_args, list) or "--repair-tool" not in raw_args:
            return event, result
        output = str(result.get("stdout") or "").strip()
        try:
            repair = json.loads(output.splitlines()[-1]) if output else {}
        except json.JSONDecodeError:
            repair = {}
        paths = repair.get("backup_extract_paths") if isinstance(repair, dict) else None
        target = str(repair.get("target_tool_id") or "") if isinstance(repair, dict) else ""
        if not isinstance(paths, list) or not paths or not target or self.channel is None:
            return event, result
        extraction_request_id = f"repair-extract-{target}-{time.time_ns()}"
        extraction_args = ["--extract-managed-backup", "--backup-owner", target]
        for path in paths:
            extraction_args.extend(("--backup-path", str(path)))
        extraction_args.append("--json")
        await asyncio.to_thread(
            self.channel.request,
            "global-cleaner",
            extraction_request_id,
            {
                "tool_id": "global-cleaner",
                "request_id": extraction_request_id,
                "_governed_command": "toolbox_request_tool_execution",
                "args": extraction_args,
            },
        )
        extraction = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            extraction = await asyncio.to_thread(
                self.channel.response,
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
        result["backup_extraction"] = extraction or {
            "status": "timed_out",
            "request_id": extraction_request_id,
        }
        result["backup_extraction_requester"] = "governance/tool/system-rescue"
        return event, result

    async def cancel(self, request_id: str) -> bool:
        return await self.cli.cancel(request_id)


__all__ = ["CentralRepairExecutor"]

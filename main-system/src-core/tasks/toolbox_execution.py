"""Tool execution request queuing and cancellation."""
from __future__ import annotations

import re
import time
from typing import Any, Dict

from .toolbox_constants import (
    MAX_TOOL_REQUEST_ID_LENGTH,
    ToolEventCallback,
)


class ExecutionMixin:
    """Tool execution request queuing and cancellation via the shared layer."""

    async def request_tool_execution(
        self,
        payload: Dict[str, Any],
        event_callback: ToolEventCallback | None = None,
    ) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}
        if (
            self.allowed_tool_ids is not None
            and tool_id not in self.allowed_tool_ids
        ):
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "TOOL_OUTSIDE_STANDALONE_SCOPE",
                "message": "This standalone runtime can execute only its own tool.",
            }
        try:
            if self.governance is None:
                raise PermissionError("PERMISSION_DENIED")
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }

        # On-demand start: if the tool was idle-stopped, auto-start it before
        # queuing the execution request so there is a process to pick it up.
        if tool_id not in self._started_request_by_tool:
            try:
                start_result = await self.start_tool(
                    {
                        "tool_id": tool_id,
                        "request_id": f"on-demand-{tool_id}-{time.time_ns()}",
                        "background": True,
                    }
                )
            except Exception:
                start_result = {"ok": False}
            if start_result.get("ok") is not True:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "error_code": "ON_DEMAND_START_FAILED",
                    "message": start_result.get("message", "ON_DEMAND_START_FAILED"),
                    "start_result": start_result,
                }

        # Notify idle manager of activity (resets the idle timer).
        if self._tool_activity_callback is not None:
            try:
                self._tool_activity_callback(tool_id)
            except Exception:
                pass

        request_id, request_error = self._tool_request_id(payload)
        if request_error is not None or request_id is None:
            return {"tool_id": tool_id, **(request_error or {})}

        try:
            self.permission_sovereign.submit_tool_execution_request(
                tool_id,
                request_id,
                dict(payload),
            )
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        return {
            "ok": True,
            "queued": True,
            "tool_id": tool_id,
            "request_id": request_id,
            "status": "queued",
            "channel": "shared-layer",
        }

    async def cancel_tool_execution(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        try:
            if self.governance is None:
                raise PermissionError("PERMISSION_DENIED")
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        requested_id = str(payload.get("request_id", "")).strip()
        if not requested_id:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MISSING_REQUEST_ID",
                "message": "Cancellation requires the exact request_id",
            }
        if len(requested_id) > MAX_TOOL_REQUEST_ID_LENGTH or any(
            ord(character) < 32 for character in requested_id
        ):
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": requested_id,
                "error_code": "INVALID_REQUEST_ID",
                "message": "request_id is too long",
            }

        try:
            cancelled = self.permission_sovereign.cancel_tool_execution_request(
                tool_id,
                requested_id,
            )
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": requested_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        return {
            "ok": cancelled,
            "tool_id": tool_id,
            "request_id": requested_id,
            "status": "cancelled" if cancelled else "not-found",
            "channel": "shared-layer",
            "error_code": None if cancelled else "REQUEST_NOT_FOUND",
        }

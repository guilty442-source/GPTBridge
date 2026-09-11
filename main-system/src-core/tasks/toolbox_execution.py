"""Tool execution request queuing and cancellation."""
from __future__ import annotations

import asyncio
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

    async def run_tool(
        self,
        payload: Dict[str, Any],
        event_callback: ToolEventCallback | None = None,
    ) -> Dict[str, Any]:
        """Queue a tool execution request and poll for the actual result.

        Unlike ``request_tool_execution`` which returns immediately after
        queuing, ``run_tool`` waits for the governed tool process to complete
        the request and returns the full result including stdout/stderr.
        This is the handler for the ``toolbox_run_tool`` command used by
        tool window frontends that expect synchronous results.
        """
        # Reuse the queueing logic, then poll for the response.
        try:
            queued = await self.request_tool_execution(payload, event_callback)
        except Exception as exc:
            return {
                "ok": False,
                "tool_id": str(payload.get("tool_id") or ""),
                "request_id": str(payload.get("request_id") or ""),
                "error_code": "QUEUE_FAILED",
                "message": f"{type(exc).__name__}: {exc}",
            }
        if queued.get("ok") is not True:
            return queued

        tool_id = str(queued.get("tool_id") or payload.get("tool_id") or "").strip()
        request_id = str(queued.get("request_id") or "").strip()
        if not tool_id or not request_id:
            return queued

        timeout_seconds = max(1.0, min(float(payload.get("timeout_seconds") or 120), 600))
        deadline = time.monotonic() + timeout_seconds
        poll_interval = 0.1

        first_poll_error: str | None = None
        while time.monotonic() < deadline:
            try:
                response = await asyncio.to_thread(
                    self.permission_sovereign.tool_execution_response,
                    tool_id,
                    request_id,
                )
                first_poll_error = None
            except (OSError, ValueError, PermissionError) as exc:
                if first_poll_error is None:
                    import traceback
                    first_poll_error = (
                        f"{type(exc).__name__}: {exc}\n"
                        + "".join(traceback.format_exception(exc))
                    )
                # The first poll may fail while the tool process is still
                # starting up or the governance token is being issued. Keep
                # retrying until the deadline; only report the error if it
                # never succeeds.
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, 0.5)
                continue

            if response is not None:
                # consume_response returns a row dict with status/response/progress.
                # Only return when the request is actually completed; otherwise
                # keep polling (the row is not deleted until completed/cancelled).
                status = response.get("status") if isinstance(response, dict) else None
                if status in ("completed", "failed", "cancelled"):
                    if isinstance(response, dict):
                        result = response.get("response")
                        if isinstance(result, dict):
                            result.setdefault("tool_id", tool_id)
                            result.setdefault("request_id", request_id)
                            return result
                        return {
                            "ok": False,
                            "tool_id": tool_id,
                            "request_id": request_id,
                            "error_code": "INVALID_TOOL_RESPONSE",
                            "message": "Tool process returned an invalid response",
                        }
                    # cancelled or completed with no response dict
                    return {
                        "ok": False,
                        "tool_id": tool_id,
                        "request_id": request_id,
                        "error_code": "TOOL_RUN_{}".format(status.upper()),
                        "message": f"Tool run status: {status}",
                    }
                # status is "queued" or "claimed" — keep polling

            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 0.5)

        if first_poll_error is not None:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "RESPONSE_POLL_FAILED",
                "message": first_poll_error,
            }

        return {
            "ok": False,
            "tool_id": tool_id,
            "request_id": request_id,
            "error_code": "TOOL_RUN_TIMEOUT",
            "message": f"Tool run timed out after {timeout_seconds}s",
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

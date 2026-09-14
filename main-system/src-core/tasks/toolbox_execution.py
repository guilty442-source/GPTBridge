"""Tool execution request queuing and cancellation."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict

from governance.registries import (
    module_assignment,
    parent_of,
    validate_execution_identity,
)

from .toolbox_constants import (
    MAX_TOOL_REQUEST_ID_LENGTH,
    ToolEventCallback,
)


class ExecutionMixin:
    """Tool execution request queuing and cancellation via the shared layer."""

    def _attested_execution_identity(self) -> str:
        """A334: the executing individual's own identity — never an echo.

        The identity is taken from an explicit runtime declaration or from a
        single-tool execution scope; requests without an attestable executor
        identity are denied by the gate instead of echoing the requested
        module code back to itself (same-value self-attestation forbidden).
        """
        declared = str(getattr(self, "execution_identity", "") or "").strip()
        if declared:
            return declared
        allowed = getattr(self, "allowed_tool_ids", None)
        if allowed is not None and len(allowed) == 1:
            return str(next(iter(allowed))).strip()
        return ""

    def _verify_module_assignment(self, tool_id: str) -> Dict[str, Any] | None:
        """A334 execution gate: the module registry is the machine authority.

        Every executable module must be registered with its exact execution
        identity, a managing sub-sovereign declared in the single-parent
        hierarchy, and decision/review authorities on record.  Returns an
        error response when the gate denies (fail-closed), otherwise None.
        """
        module_code = tool_id.upper().replace("-", "_")
        try:
            row = module_assignment(module_code)
        except (OSError, KeyError, ValueError, RuntimeError) as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MODULE_REGISTRY_UNAVAILABLE",
                "message": f"A334 module-assignment registry is unavailable: {type(error).__name__}",
            }
        if row is None:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MODULE_NOT_IN_REGISTRY",
                "message": "A334: executable module is not registered",
            }
        execution_identity = self._attested_execution_identity()
        if not execution_identity:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "EXECUTION_IDENTITY_NOT_ATTESTED",
                "message": "A334: executing individual identity is not attested",
            }
        if not validate_execution_identity(module_code, execution_identity):
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "EXECUTION_IDENTITY_MISMATCH",
                "message": "A334: execution identity does not match registry",
            }
        managing = str(row.get("managing_sub_sovereign") or "")
        if not managing or parent_of(managing) is None:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MANAGING_SUB_SOVEREIGN_UNREGISTERED",
                "message": "A334: managing sub-sovereign is not in the hierarchy registry",
            }
        if not row.get("decision_authority") or not row.get("review_authority"):
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MODULE_AUTHORITY_INCOMPLETE",
                "message": "A334: module decision/review authority is incomplete",
            }
        return None

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
        # A334: the module-assignment registry is the execution gate —
        # verify the exact registered execution identity, the managing
        # sub-sovereign's hierarchy registration, and the module's
        # decision/review authorities before any governed execution.
        assignment_error = self._verify_module_assignment(tool_id)
        if assignment_error is not None:
            return assignment_error
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

        channel_tool_id = self._channel_target_tool_id(tool_id)
        try:
            self.permission_sovereign.submit_tool_execution_request(
                channel_tool_id,
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

        channel_tool_id = self._channel_target_tool_id(tool_id)
        timeout_seconds = max(1.0, min(float(payload.get("timeout_seconds") or 120), 600))
        deadline = time.monotonic() + timeout_seconds
        poll_interval = 0.1

        first_poll_error: str | None = None
        while time.monotonic() < deadline:
            try:
                response = await asyncio.to_thread(
                    self.permission_sovereign.tool_execution_response,
                    channel_tool_id,
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
                self._channel_target_tool_id(tool_id),
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

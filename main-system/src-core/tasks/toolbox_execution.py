"""Tool execution request queuing and cancellation."""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Dict

from governance.registries import (
    module_assignment,
    parent_of,
    validate_execution_identity,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)

from .toolbox_constants import (
    MAX_TOOL_REQUEST_ID_LENGTH,
    ToolEventCallback,
)


class ExecutionMixin:
    """Tool execution request queuing and cancellation via the shared layer."""

    def _module_code_for_identity(self, identity: str) -> str:
        """Resolve the codex module architecture code for a governed identity.

        The naive ``tool_id.upper().replace("-", "_")`` derivation is only
        valid for the *hosting* runtime identity.  Companions, nested sealed
        identities (e.g. ``xingcheng`` inside ``local-model``) and resident
        authority modules first resolve through the sealed manifest binding
        in the identity registry to their hosting tool; unresolvable
        identities keep the derived code so the registry lookup fails closed.
        """
        code = str(identity).strip().upper().replace("-", "_")
        try:
            if module_assignment(code) is not None:
                return code
            identities = identity_group_snapshot().identities
        except (OSError, KeyError, ValueError, RuntimeError):
            return code
        for bound in identities:
            if str(getattr(bound, "bound_tool_id", "") or "").strip() != identity:
                continue
            binding = getattr(bound, "manifest_binding", None)
            template = str(getattr(binding, "path_template", "") or "")
            if not getattr(binding, "required", False) or not template:
                continue
            parts = Path(template.format(tool_id=identity)).parts
            host = ""
            if "Standalone tools" in parts:
                index = parts.index("Standalone tools")
                if len(parts) > index + 1:
                    host = parts[index + 1]
            elif len(parts) >= 2:
                host = parts[0]
            if host:
                host_code = host.upper().replace("-", "_")
                try:
                    if module_assignment(host_code) is not None:
                        return host_code
                except (OSError, KeyError, ValueError, RuntimeError):
                    pass
        return code

    def _bound_manifest_identity(self, channel_id: str, tool_dir: Path) -> str:
        """Read the sealed-registry bound manifest's declared identity.

        For a nested governed identity (e.g. ``xingcheng`` inside
        ``local-model``) the manifest pinned by the identity registry must
        declare that identity — proving the channel claimant is the bound
        identity rather than a self-asserted name.
        """
        try:
            identities = identity_group_snapshot().identities
        except (OSError, KeyError, ValueError, RuntimeError):
            return ""
        for bound in identities:
            if str(getattr(bound, "bound_tool_id", "") or "").strip() != channel_id:
                continue
            binding = getattr(bound, "manifest_binding", None)
            template = str(getattr(binding, "path_template", "") or "")
            id_field = str(getattr(binding, "tool_id_field", "") or "id")
            if not getattr(binding, "required", False) or not template:
                continue
            manifest_path = (
                self.project_root / template.format(tool_id=channel_id)
            ).resolve()
            try:
                manifest_path.relative_to(Path(tool_dir).resolve())
            except ValueError:
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            declared = str(manifest.get(id_field) or "").strip()
            if declared:
                return declared
        return ""

    def _attested_execution_identity(self, tool_id: str) -> str:
        """A334: the executing individual's attested identity — never an echo.

        The identity is resolved through the authenticated chain, never
        copied from the request: the identity that actually claims the
        shared-layer channel (sealed-registry bound, the same identity the
        governance bootstrap token is minted for) is resolved to its
        hosting module's registered execution identity.  A standalone
        runtime scoped to a single tool attests that tool's chain.
        """
        source = str(tool_id).strip()
        allowed = getattr(self, "allowed_tool_ids", None)
        if allowed is not None and len(allowed) == 1:
            source = str(next(iter(allowed))).strip() or source
        if not source:
            return ""
        channel_id = self._channel_target_tool_id(source)
        if not channel_id:
            return ""
        return self._module_code_for_identity(channel_id)

    def _verify_module_assignment(self, tool_id: str) -> Dict[str, Any] | None:
        """A334 execution gate: the module registry is the machine authority.

        Four-way identity consistency, fail-closed: the manifest declares
        the requested tool id; the channel claimant is the sealed-registry
        governed identity (the identity the governance bootstrap token is
        bound to at launch); a nested channel identity declares itself in
        its registry-pinned bound manifest; and the module code derived
        from the requested tool must match the registered execution
        identity derived independently from the bound channel identity.

        Returns an error response when the gate denies, otherwise None.
        """
        try:
            manifest, tool_dir = self._load_manifest_cached(tool_id)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "TOOL_MANIFEST_UNAVAILABLE",
                "message": f"A334: tool manifest is unavailable: {type(error).__name__}",
            }
        if not isinstance(manifest, dict) or str(
            manifest.get("id") or ""
        ).strip() != tool_id:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MANIFEST_IDENTITY_MISMATCH",
                "message": "A334: manifest identity does not match the tool id",
            }
        try:
            owner = self._runtime_owner_tool_id(tool_id, manifest)
            channel_id = str(
                self._governed_runtime_tool_id(owner) or ""
            ).strip()
        except PermissionError:
            channel_id = ""
        except (OSError, KeyError, ValueError, RuntimeError):
            channel_id = ""
        if not channel_id:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "RUNTIME_IDENTITY_UNRESOLVED",
                "message": "A334: no sealed-registry identity claims the channel",
            }
        if channel_id != owner and self._bound_manifest_identity(
            channel_id, tool_dir
        ) != channel_id:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "CHANNEL_MANIFEST_IDENTITY_MISMATCH",
                "message": "A334: bound manifest does not declare the channel identity",
            }
        module_code = self._module_code_for_identity(tool_id)
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
        execution_identity = self._attested_execution_identity(tool_id)
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

"""Process-state management and authorization helpers for ToolboxService."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.registries.permissions.tool_routes import (
    AUTHORIZED_TOOL_IDS,
)


class ProcessMixin:
    """Process ownership tracking, lifecycle authorization, and governance helpers."""

    # ------------------------------------------------------------------
    # Runtime ownership / authorization
    # ------------------------------------------------------------------

    def _governed_runtime_tool_id(
        self,
        tool_id: str,
        manifest: Dict[str, Any] | None = None,
    ) -> str:
        """Resolve the sealed-registry identity a tool's source runtime runs as.

        A tool directory may host nested identity domains — e.g.
        ``local-model`` physically hosts the ``xingcheng`` governed
        endpoint.  The runtime identity is the registered AI-channel
        participant whose sealed-registry bound manifest lives inside this
        tool directory.  Registry-bound only: a tool cannot claim a
        foreign identity through its own manifest.  More than one nested
        channel participant is ambiguous and denied.
        """
        try:
            tool_dir = self._tool_directory_for_id(tool_id)
        except ValueError:
            return tool_id
        tool_root = tool_dir.resolve()
        candidates: list[str] = []
        for identity in identity_group_snapshot().identities:
            bound_id = str(identity.bound_tool_id or "").strip()
            if bound_id == tool_id or bound_id not in AUTHORIZED_TOOL_IDS:
                continue
            binding = identity.manifest_binding
            if not binding.required or not binding.path_template:
                continue
            bound_manifest = (
                self.project_root / binding.path_template.format(tool_id=bound_id)
            ).resolve()
            try:
                bound_manifest.relative_to(tool_root)
            except ValueError:
                continue
            if bound_manifest.is_file():
                candidates.append(bound_id)
        if len(candidates) > 1:
            raise PermissionError("PERMISSION_DENIED")
        return candidates[0] if candidates else tool_id

    def _channel_target_tool_id(self, tool_id: str) -> str:
        """Resolve the governed identity that actually claims the channel.

        Companions route through their shared owner runtime, and the owner
        may authenticate under a nested sealed-registry identity (e.g.
        ``local-model`` -> ``xingcheng``).  Requests must be addressed to
        the identity that claims the channel; the external tool_id is
        preserved in result objects.
        """
        try:
            manifest, _tool_dir = self._load_manifest_cached(tool_id)
            owner = self._runtime_owner_tool_id(tool_id, manifest)
            if owner != tool_id:
                manifest, _tool_dir = self._load_manifest_cached(owner)
            return self._governed_runtime_tool_id(owner, manifest)
        except PermissionError:
            raise
        except Exception:
            return tool_id


    def _runtime_owner_tool_id(
        self,
        tool_id: str,
        manifest: Dict[str, Any] | None = None,
    ) -> str:
        if manifest is None:
            try:
                tool_dir = self._tool_directory_for_id(tool_id)
                manifest = json.loads(
                    (tool_dir / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                return tool_id
            if not isinstance(manifest, dict):
                return tool_id
        owner = str(manifest.get("runtime_owner_tool_id") or "").strip()
        if not owner or owner == tool_id:
            return tool_id
        # The runtime owner is declared either as the tool's host or as its
        # shared permission owner — a physical host folder may host a
        # companion while a different identity owns the runtime (e.g.
        # star-chat physically nested under local-model but running on the
        # xingcheng runtime).
        declared_owners = {
            str(manifest.get("host_tool_id") or "").strip(),
            str(manifest.get("shared_permission_owner") or "").strip(),
        }
        # A tool may also delegate its runtime to a companion it declares
        # (e.g. model-dialogue opens on the lightweight star-chat runtime so
        # the local model is never a start prerequisite of the window).
        companions = manifest.get("companion_tools")
        if isinstance(companions, list):
            for declaration in companions:
                if isinstance(declaration, dict):
                    declared_owners.add(str(declaration.get("id") or "").strip())
        declared_owners.discard("")
        if owner not in declared_owners:
            raise PermissionError("PERMISSION_DENIED")
        try:
            owner_dir = self._tool_directory_for_id(owner)
        except ValueError as error:
            raise PermissionError("PERMISSION_DENIED") from error
        if not (owner_dir / "manifest.json").is_file():
            raise PermissionError("PERMISSION_DENIED")
        return owner

    def _authorize_tool_lifecycle(self, tool_id: str, action: str) -> None:
        if self.governance is None:
            raise PermissionError("PERMISSION_DENIED")
        if action.casefold() in {"stop", "force-close", "force_close"}:
            try:
                manifest, _tool_dir = self._load_manifest_cached(tool_id)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise PermissionError("PERMISSION_DENIED") from error
            lifecycle = manifest.get("lifecycle")
            if isinstance(lifecycle, dict) and lifecycle.get("stoppable") is False:
                raise PermissionError("LIFECYCLE_LOCKED")
        authority_tool_id = self._runtime_owner_tool_id(tool_id)
        self.permission_sovereign.authorize_tool_lifecycle(authority_tool_id, action)

    @staticmethod
    def _governance_reason(check: Dict[str, Any]) -> str:
        reason = str(check.get("reason", "")).strip()
        if reason:
            return reason
        error_report = check.get("error_report")
        if isinstance(error_report, dict):
            root_cause = str(error_report.get("root_cause", "")).strip()
            if root_cause:
                return root_cause
        return "unknown governance rule"

    def _governance_blocked(self, check: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": False, "message": f"GOVERNANCE BLOCKED: {self._governance_reason(check)}"}

    # ------------------------------------------------------------------
    # Process-state lock helpers
    # ------------------------------------------------------------------

    async def _reserve_tool_process(
        self,
        *,
        request_id: str,
        tool_id: str,
        kind: str,
    ) -> Dict[str, Any] | None:
        async with self._process_state_lock:
            duplicate_tool_id = self._request_tool_ids.get(request_id)
            if duplicate_tool_id is not None:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "active_tool_id": duplicate_tool_id,
                    "error_code": "DUPLICATE_REQUEST_ID",
                    "message": "request_id is already active",
                }
            active_request_id = self._active_request_by_tool.get(tool_id)
            if active_request_id is not None:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "active_request_id": active_request_id,
                    "error_code": "TOOL_BUSY",
                    "message": "Another process is already active for this tool",
                }
            self._request_tool_ids[request_id] = tool_id
            self._request_kinds[request_id] = kind
            self._active_request_by_tool[tool_id] = request_id
        return None

    async def _register_tool_process(
        self,
        request_id: str,
        process: asyncio.subprocess.Process,
    ) -> bool:
        async with self._process_state_lock:
            if request_id not in self._request_tool_ids:
                return True
            self._running_processes[request_id] = process
            tool_id = self._request_tool_ids.get(request_id)
            if tool_id and self._request_kinds.get(request_id) == "started":
                self._started_request_by_tool[tool_id] = request_id
            return request_id in self._cancelled_request_ids

    async def _release_tool_process(self, request_id: str) -> bool:
        async with self._process_state_lock:
            tool_id = self._request_tool_ids.pop(request_id, None)
            self._request_kinds.pop(request_id, None)
            self._running_processes.pop(request_id, None)
            cancelled = request_id in self._cancelled_request_ids
            self._cancelled_request_ids.discard(request_id)
            if tool_id and self._active_request_by_tool.get(tool_id) == request_id:
                self._active_request_by_tool.pop(tool_id, None)
            if tool_id and self._started_request_by_tool.get(tool_id) == request_id:
                self._started_request_by_tool.pop(tool_id, None)
            return cancelled

    async def _release_started_tool_command_slot(self, request_id: str) -> None:
        """Let a standalone GUI issue commands while its EXE remains open."""

        async with self._process_state_lock:
            if self._request_kinds.get(request_id) != "started":
                return
            tool_id = self._request_tool_ids.get(request_id)
            if tool_id and self._active_request_by_tool.get(tool_id) == request_id:
                self._active_request_by_tool.pop(tool_id, None)

    async def _active_tool_process(
        self,
        tool_id: str,
    ) -> tuple[str | None, asyncio.subprocess.Process | None, str | None]:
        async with self._process_state_lock:
            request_id = self._active_request_by_tool.get(tool_id)
            if request_id is None:
                return None, None, None
            return (
                request_id,
                self._running_processes.get(request_id),
                self._request_kinds.get(request_id),
            )

    async def _started_tool_process(
        self,
        tool_id: str,
    ) -> tuple[str | None, asyncio.subprocess.Process | None]:
        async with self._process_state_lock:
            request_id = self._started_request_by_tool.get(tool_id)
            if request_id is None:
                return None, None
            return request_id, self._running_processes.get(request_id)

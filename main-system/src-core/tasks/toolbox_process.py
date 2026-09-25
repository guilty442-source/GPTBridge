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
from governance_rule.permission_directory.code_rule_directory import (
    code_rule_directory_snapshot,
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
        declared_runtime_owners = dict(
            code_rule_directory_snapshot().runtime_owner_tool_bindings
        )
        declared_owner = str(declared_runtime_owners.get(tool_id) or "").strip()
        if declared_owner:
            if declared_owner != tool_id:
                raise PermissionError("PERMISSION_DENIED")
            return declared_owner
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
                relative_parts = bound_manifest.parent.relative_to(tool_root).parts
            except ValueError:
                continue
            # A binding nested inside a deeper tool root belongs to that
            # sub-tool (e.g. star-chat lives under model-dialogue, which is
            # itself nested under local-model); only direct participants of
            # this tool directory count here.
            if any(
                (tool_root.joinpath(*relative_parts[:depth]) / "manifest.json").is_file()
                for depth in range(1, len(relative_parts))
            ):
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

    def _authorize_tool_lifecycle(
        self,
        tool_id: str,
        action: str,
        *,
        allow_locked: bool = False,
    ) -> None:
        if self.governance is None:
            raise PermissionError("PERMISSION_DENIED")
        if (
            not allow_locked
            and action.casefold() in {"stop", "force-close", "force_close"}
        ):
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
            cancelled = request_id in self._cancelled_request_ids
        registry = getattr(self, "_process_registry", None)
        if registry is not None and tool_id:
            try:
                registry.register(
                    process.pid,
                    module_id=str(tool_id),
                    request_id=request_id,
                    owned=True,
                )
            except Exception:  # registry 失敗不得中斷工具啟動
                pass
        return cancelled

    async def _release_tool_process(self, request_id: str) -> bool:
        async with self._process_state_lock:
            tool_id = self._request_tool_ids.pop(request_id, None)
            self._request_kinds.pop(request_id, None)
            process = self._running_processes.pop(request_id, None)
            cancelled = request_id in self._cancelled_request_ids
            self._cancelled_request_ids.discard(request_id)
            if tool_id and self._active_request_by_tool.get(tool_id) == request_id:
                self._active_request_by_tool.pop(tool_id, None)
            if tool_id and self._started_request_by_tool.get(tool_id) == request_id:
                self._started_request_by_tool.pop(tool_id, None)
        registry = getattr(self, "_process_registry", None)
        if registry is not None and process is not None:
            try:
                if process.returncode is None:
                    registry.mark_shutdown(process.pid, "released")
                else:
                    registry.mark_shutdown(process.pid, "exited")
            except Exception:
                pass
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

    async def tool_process_active(self, tool_id: str) -> bool:
        """True when a live governed process currently backs ``tool_id``."""
        _request_id, process, _kind = await self._active_tool_process(tool_id)
        if process is not None and process.returncode is None:
            return True
        if self._started_request_by_tool.get(tool_id):
            return True
        # A governed runtime may have been started outside this service's
        # spawn tracking (another backend instance, a governed supervisor,
        # or an earlier backend generation whose process outlived it).  The
        # start path's already-running branch returns ``ok`` without
        # populating ``_started_request_by_tool``, so relying on the spawn
        # maps alone reports a false cold state and permanently wedges
        # governed consumers such as the codex-amendment intake's
        # Xingcheng search wiring.  Verify real process liveness from the
        # manifest-resolved source-runtime entry instead.
        return await asyncio.to_thread(
            self._external_runtime_active, tool_id
        )

    def _external_runtime_active(self, tool_id: str) -> bool:
        """Best-effort liveness probe for externally started tool runtimes.

        Fail-closed: any resolution or scan failure reports inactive so a
        caller never mistakes an unknown state for a live governed channel.
        """
        try:
            manifest, tool_dir = self._load_manifest_cached(tool_id)
            if not self._has_governed_source_runtime(manifest):
                return False
            try:
                entry = self._resolve_special_unpacked_entry(
                    manifest, tool_dir
                )
            except ValueError:
                return False
            return bool(self._running_source_runtime_process_ids(entry))
        except Exception:  # noqa: BLE001 — unknown means inactive
            return False

    async def _started_tool_process(
        self,
        tool_id: str,
    ) -> tuple[str | None, asyncio.subprocess.Process | None]:
        async with self._process_state_lock:
            request_id = self._started_request_by_tool.get(tool_id)
            if request_id is None:
                return None, None
            return request_id, self._running_processes.get(request_id)

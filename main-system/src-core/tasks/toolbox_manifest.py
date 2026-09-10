"""Manifest loading, tool records, path resolution, listing, and status."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

from .tool_path_resolver import ToolPathResolver
from .tool_process_registry import (
    batch_running_status,
    running_executable_process_ids,
    running_packaged_backend_process_ids,
    running_source_runtime_process_ids,
    running_source_ui_process_ids,
    stop_running_executable,
    stop_running_packaged_backend,
    stop_running_source_runtime,
    stop_running_source_ui,
)


class ManifestMixin:
    """Manifest loading, tool-record construction, path resolution, and listing."""

    # ------------------------------------------------------------------
    # Path-resolution delegates (thin wrappers around ToolPathResolver)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_entry_content(tool_name: str) -> str:
        return (
            f"\"\"\"{tool_name} tool entry.\"\"\"\n\n"
            "def main() -> None:\n"
            f"    print(\"{tool_name} is ready on Windows 11\")\n\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
        )

    @staticmethod
    def _is_link_or_reparse_point(path: Path) -> bool:
        return ToolPathResolver.is_link_or_reparse_point(path)

    def _validated_tool_directory(self, tool_dir: Path) -> Path:
        return self._path_resolver.validated_tool_directory(tool_dir)

    def _validated_tool_path(
        self,
        tool_dir: Path,
        candidate: Path,
        *,
        label: str,
    ) -> Path:
        return self._path_resolver.validated_tool_path(
            tool_dir,
            candidate,
            label=label,
        )

    def _resolve_entry_file(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        return self._path_resolver.resolve_entry_file(manifest, tool_dir)

    def _resolve_working_directory(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        return self._path_resolver.resolve_working_directory(manifest, tool_dir)

    def _resolve_executable_file(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        return self._path_resolver.resolve_executable_file(manifest, tool_dir)

    def _resolve_python_executable(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        return self._path_resolver.resolve_python_executable(manifest, tool_dir)

    @staticmethod
    def _is_special_unpacked(manifest: Dict[str, Any]) -> bool:
        return ToolPathResolver.is_special_unpacked(manifest)

    @staticmethod
    def _has_governed_background_source(manifest: Dict[str, Any]) -> bool:
        return ToolPathResolver.has_governed_background_source(manifest)

    @staticmethod
    def _has_governed_source_runtime(manifest: Dict[str, Any]) -> bool:
        return ToolPathResolver.has_governed_source_runtime(manifest)

    @staticmethod
    def _is_dual_runtime(manifest: Dict[str, Any]) -> bool:
        return ToolPathResolver.is_dual_runtime(manifest)

    def _resolve_special_unpacked_entry(
        self,
        manifest: Dict[str, Any],
        tool_dir: Path,
    ) -> Path:
        return self._path_resolver.resolve_special_unpacked_entry(
            manifest,
            tool_dir,
        )

    # ------------------------------------------------------------------
    # Missing-tool helpers
    # ------------------------------------------------------------------

    def _forget_missing_tool(self, tool_id: str) -> None:
        # Discovery is read-only. Missing tools are reflected directly from
        # the manifest directory and never persisted by the main system.
        return None

    @staticmethod
    def _missing_tool_result(tool_id: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": "舊應用程式資料已移除，請重新整理應用程式清單。",
            "removed": True,
        }

    # ------------------------------------------------------------------
    # Manifest-to-record conversion
    # ------------------------------------------------------------------

    def _manifest_to_record(self, tool_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
        manifest_path = tool_dir / "manifest.json"
        entry_file = self._resolve_entry_file(manifest, tool_dir)
        executable_file = self._resolve_executable_file(manifest, tool_dir)
        source_runtime = self._has_governed_source_runtime(manifest)
        source_runtime_entry = (
            self._resolve_special_unpacked_entry(manifest, tool_dir)
            if source_runtime
            else None
        )
        record = dict(manifest)
        locale_path = tool_dir / "locales" / "zh-TW.json"
        try:
            locale = json.loads(locale_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            locale = {}
        if isinstance(locale, dict):
            name_key = str(manifest.get("name_key", "")).strip()
            description_key = str(manifest.get("description_key", "")).strip()
            localized_name = locale.get(name_key) if name_key else None
            localized_description = locale.get(description_key) if description_key else None
            if isinstance(localized_name, str) and localized_name.strip():
                record["name"] = localized_name.strip()
            if isinstance(localized_description, str) and localized_description.strip():
                record["description"] = localized_description.strip()
        record["folder_path"] = str(tool_dir)
        record["manifest_path"] = str(manifest_path)
        record["code_path"] = str(entry_file)
        record["standalone"] = True
        permissions = manifest.get("permissions")
        permissions = permissions if isinstance(permissions, dict) else {}
        record["data_boundary"] = {
            "standalone": True,
            "code_scope": str(permissions.get("code_scope") or "").strip(),
            "database_scope": str(permissions.get("database_scope") or "").strip(),
        }
        record["executable_path"] = str(executable_file)
        record["executable_exists"] = executable_file.exists()
        launch = manifest.get("launch")
        primary_launch = (
            str(launch.get("primary") or "").strip().casefold()
            if isinstance(launch, dict)
            else ""
        )
        # Independent tools always use governed native source code; EXE is not a startup option.
        if source_runtime:
            record["automatic_runtime_mode"] = "governed-source"
        else:
            record["automatic_runtime_mode"] = "executable"
        record["runtime_mode"] = (
            str(launch.get("mode") or "").strip()
            if isinstance(launch, dict) and launch.get("mode")
            else record["automatic_runtime_mode"]
        )
        record["runtime_available"] = source_runtime_entry is not None
        if source_runtime_entry is not None:
            record["source_runtime_entry"] = str(source_runtime_entry)
        # Folder size is populated by the trusted Electron main process. Do
        # not send a false zero because the renderer treats numeric values as
        # authoritative and would skip the local inventory fallback.
        record["project_size_bytes"] = None
        return record

    # ------------------------------------------------------------------
    # Companion tool discovery
    # ------------------------------------------------------------------

    def _declared_companion_tool_directories(self) -> list[Path]:
        companions: list[Path] = []
        if not self.tools_dir.exists():
            return companions
        for host_dir in sorted(self.tools_dir.iterdir(), key=lambda item: item.name.lower()):
            host_manifest_path = host_dir / "manifest.json"
            if not host_dir.is_dir() or not host_manifest_path.is_file():
                continue
            try:
                host_manifest = json.loads(host_manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            declarations = host_manifest.get("companion_tools")
            if not isinstance(declarations, list):
                continue
            for declaration in declarations:
                if not isinstance(declaration, dict):
                    continue
                relative_path = str(declaration.get("path") or "").strip()
                declared_id = str(declaration.get("id") or "").strip()
                if not relative_path or not declared_id:
                    continue
                candidate = host_dir / relative_path
                try:
                    candidate = self._validated_tool_directory(candidate)
                    manifest = json.loads(
                        (candidate / "manifest.json").read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if manifest.get("id") != declared_id:
                    continue
                companions.append(candidate)
        return companions

    # ------------------------------------------------------------------
    # Manifest caching and tool-directory resolution
    # ------------------------------------------------------------------

    def _load_manifest_cached(self, tool_id: str) -> tuple[Dict[str, Any], Path]:
        """Load and cache a tool's manifest, returning (manifest, tool_dir).

        Avoids redundant manifest.json reads during startup — a single
        tool start previously triggered 3+ manifest reads.
        """
        cached = self._manifest_cache.get(tool_id)
        if cached is not None:
            return cached
        tool_dir = self._tool_directory_for_id(tool_id)
        manifest = json.loads(
            (tool_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self._manifest_cache[tool_id] = (manifest, tool_dir)
        return manifest, tool_dir

    def _build_tool_dir_index(self) -> dict[str, str]:
        """Build a one-time index of tool_dir_name -> tool_id.

        Replaces the O(n) scan in _tool_directory_for_id with an O(1)
        lookup after the first call.
        """
        if self._tool_dir_index is not None:
            return self._tool_dir_index
        index: dict[str, str] = {}
        if self.tools_dir.exists():
            for candidate in self.tools_dir.iterdir():
                manifest_path = candidate / "manifest.json"
                if not candidate.is_dir() or not manifest_path.is_file():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                tid = str(manifest.get("id") or "").strip()
                if tid:
                    index[candidate.name] = tid
                    self._manifest_cache[tid] = (manifest, candidate)
        self._tool_dir_index = index
        return index

    def _tool_directory_for_id(self, tool_id: str) -> Path:
        direct = self.tools_dir / tool_id
        if (direct / "manifest.json").is_file():
            return self._validated_tool_directory(direct)
        # Use the cached index instead of scanning every directory each time.
        index = self._build_tool_dir_index()
        for dir_name, tid in index.items():
            if tid == tool_id:
                return self._validated_tool_directory(self.tools_dir / dir_name)
        for companion in self._declared_companion_tool_directories():
            try:
                manifest = json.loads(
                    (companion / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("id") == tool_id:
                self._manifest_cache[tool_id] = (manifest, companion)
                return companion
        raise ValueError(f"Tool directory is unavailable: {tool_id}")

    def _load_manifest_records(self) -> list[Dict[str, Any]]:
        records: list[Dict[str, Any]] = []
        if not self.tools_dir.exists():
            return records

        for tool_dir in sorted(self.tools_dir.iterdir(), key=lambda item: item.name.lower()):
            manifest_path = tool_dir / "manifest.json"
            if not tool_dir.is_dir() or not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                records.append(self._manifest_to_record(tool_dir, manifest))
            except (OSError, ValueError):
                continue
        known_ids = {str(record.get("id") or "") for record in records}
        for tool_dir in self._declared_companion_tool_directories():
            try:
                manifest = json.loads((tool_dir / "manifest.json").read_text(encoding="utf-8"))
                tool_id = str(manifest.get("id") or "")
                if tool_id and tool_id not in known_ids:
                    records.append(self._manifest_to_record(tool_dir, manifest))
                    known_ids.add(tool_id)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return records

    # ------------------------------------------------------------------
    # Listing and status
    # ------------------------------------------------------------------

    _HIDDEN_INFRASTRUCTURE_IDS = frozenset(
        {"global-cleaner", "governance_rule", "shared-layer"}
    )
    _RESIDENT_SERVICE_IDS = frozenset({"shared-layer", "xingcheng"})

    def _classify_tool(self, tool: dict[str, Any]) -> bool:
        """Classify a tool record in-place. Return True if it needs process checks."""
        tool_id = str(tool.get("id", "")).strip()
        if tool_id in self._HIDDEN_INFRASTRUCTURE_IDS:
            tool["hidden_from_toolbox"] = True
        if tool_id in self._RESIDENT_SERVICE_IDS:
            tool["permission_denied"] = False
            tool["lifecycle_locked"] = True
            tool["resident_service"] = True
            tool["status"] = "running"
            return False
        lifecycle = tool.get("lifecycle") or {}
        tool["resident_service"] = lifecycle.get("stoppable") is False
        try:
            authority_tool_id = self._runtime_owner_tool_id(tool_id, tool)
            authorized = bool(
                tool_id and self.permission_sovereign.can_start_tool(authority_tool_id)
            )
        except PermissionError:
            authorized = False
        tool["permission_denied"] = not authorized
        tool["status"] = "stopped"
        return authorized

    async def list_tools(self) -> Dict[str, Any]:
        tools = self._load_manifest_records()
        # First pass: classify tools and determine which need process checks.
        # Tools that are not authorized skip process detection entirely.
        tools_needing_checks: list[dict[str, Any]] = []
        for tool in tools:
            if self._classify_tool(tool):
                tools_needing_checks.append(tool)
        # Batch process check: single PowerShell call for all authorized tools,
        # run in a thread to avoid blocking the async event loop.
        if tools_needing_checks:
            batch = await asyncio.to_thread(batch_running_status, tools_needing_checks)
            for tool in tools_needing_checks:
                tool_id = str(tool.get("id", "")).strip()
                entry = batch.get(tool_id, {})
                running = bool(
                    entry.get("source_runtime")
                    or entry.get("executable")
                    or entry.get("source_ui")
                )
                tool["status"] = "running" if running else "stopped"
        return {"ok": True, "tools": tools}

    async def update_status(self, tool_id: str, status: str) -> Dict[str, Any]:
        if not self._maintenance_ready:
            return self._maintenance_not_ready_result("update_status")
        try:
            manifest_path = self._tool_directory_for_id(tool_id) / "manifest.json"
        except ValueError:
            self._forget_missing_tool(tool_id)
            return self._missing_tool_result(tool_id)
        # Runtime status is process-derived and intentionally not stored in a
        # shared database. Each independent tool owns its own business state.
        return {"ok": True, "tool_id": tool_id, "status": status}

    # ------------------------------------------------------------------
    # Process-registry static-method aliases
    # ------------------------------------------------------------------

    _running_executable_process_ids = staticmethod(running_executable_process_ids)
    _running_source_runtime_process_ids = staticmethod(
        running_source_runtime_process_ids
    )
    _running_packaged_backend_process_ids = staticmethod(
        running_packaged_backend_process_ids
    )
    _running_source_ui_process_ids = staticmethod(running_source_ui_process_ids)
    _stop_running_source_ui = staticmethod(stop_running_source_ui)
    _stop_running_executable = staticmethod(stop_running_executable)
    _stop_running_source_runtime = staticmethod(stop_running_source_runtime)
    _stop_running_packaged_backend = staticmethod(stop_running_packaged_backend)

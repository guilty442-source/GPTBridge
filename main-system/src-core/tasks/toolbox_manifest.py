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
from .toolbox_manifest_records import ManifestRecordMixin


class ManifestMixin(ManifestRecordMixin):
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
    # Manifest caching and tool-directory resolution
    # ------------------------------------------------------------------

    def _load_manifest_cached(self, tool_id: str) -> tuple[Dict[str, Any], Path]:
        """Load a tool's manifest, reloading when the file changes on disk.

        Avoids redundant manifest.json reads during startup — a single
        tool start previously triggered 3+ manifest reads.  The cache is
        mtime/size-fenced so a manifest edit (for example flipping
        ``lifecycle.stoppable``) is visible to the running system without a
        backend restart, which is what keeps the tool surface current.
        """
        cached = self._manifest_cache.get(tool_id)
        if cached is not None:
            manifest, tool_dir = cached
            key = self._manifest_freshness_key(tool_dir)
            if key is not None and self._manifest_cache_keys.get(tool_id) == key:
                return manifest, tool_dir
        tool_dir = self._tool_directory_for_id(tool_id)
        manifest = json.loads(
            (tool_dir / "manifest.json").read_text(encoding="utf-8")
        )
        if not isinstance(manifest, dict):
            raise ValueError(f"manifest is not a mapping: {tool_id}")
        self._manifest_cache[tool_id] = (manifest, tool_dir)
        key = self._manifest_freshness_key(tool_dir)
        if key is not None:
            self._manifest_cache_keys[tool_id] = key
        return manifest, tool_dir

    @staticmethod
    def _manifest_freshness_key(tool_dir: Path) -> tuple[int, int] | None:
        try:
            stat_result = (tool_dir / "manifest.json").stat()
        except OSError:
            return None
        return (stat_result.st_mtime_ns, stat_result.st_size)

    def _build_tool_dir_index(self) -> dict[str, str]:
        """Build a one-time index of tool_dir_name -> tool_id.

        Replaces the O(n) scan in _tool_directory_for_id with an O(1)
        lookup after the first call.
        """
        if self._tool_dir_index is not None:
            return self._tool_dir_index
        index: dict[str, str] = {}
        scan_dirs = [self.tools_dir]
        if self.project_root != self.tools_dir and self.project_root.is_dir():
            scan_dirs.append(self.project_root)
        for scan_dir in scan_dirs:
            for manifest_path in scan_dir.rglob("manifest.json"):
                candidate = manifest_path.parent
                # Skip the Standalone tools directory itself when scanning root
                if scan_dir == self.project_root and candidate == self.tools_dir:
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(manifest, dict):
                    continue
                tid = str(manifest.get("id") or "").strip()
                if tid:
                    index[candidate.name] = tid
                    self._manifest_cache[tid] = (manifest, candidate)
                    key = self._manifest_freshness_key(candidate)
                    if key is not None:
                        self._manifest_cache_keys[tid] = key
        self._tool_dir_index = index
        return index

    def _tool_directory_for_id(self, tool_id: str) -> Path:
        direct = self.tools_dir / tool_id
        if (direct / "manifest.json").is_file():
            return self._validated_tool_directory(direct)
        # A278/A280: resident services (governance_rule, shared-layer)
        # remain at the project root, not under "Standalone tools/".
        root_direct = self.project_root / tool_id
        if (root_direct / "manifest.json").is_file():
            return self._validated_tool_directory(root_direct)
        # Use the manifest cache which has the correct path for nested tools.
        cached = self._manifest_cache.get(tool_id)
        if cached is not None:
            _manifest, tool_dir = cached
            return self._validated_tool_directory(tool_dir)
        # Use the cached index instead of scanning every directory each time.
        index = self._build_tool_dir_index()
        # _build_tool_dir_index() populates _manifest_cache with the real
        # directory of every discovered manifest — prefer it so nested
        # companion tools resolve to their actual path instead of a
        # nonexistent top-level directory of the same name.
        cached = self._manifest_cache.get(tool_id)
        if cached is not None:
            _cached_manifest, tool_dir = cached
            return self._validated_tool_directory(tool_dir)
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
            if isinstance(manifest, dict) and manifest.get("id") == tool_id:
                self._manifest_cache[tool_id] = (manifest, companion)
                key = self._manifest_freshness_key(companion)
                if key is not None:
                    self._manifest_cache_keys[tool_id] = key
                return companion
        raise ValueError(f"Tool directory is unavailable: {tool_id}")


    # ------------------------------------------------------------------
    # Listing and status
    # ------------------------------------------------------------------

    _RESIDENT_SERVICE_IDS = frozenset({"shared-layer", "xingcheng"})

    @staticmethod
    def _declares_independent_tool_card(tool: dict[str, Any]) -> bool:
        """A manifest owns a toolbox card only when it declares itself as an
        independent tool.  Infrastructure (the governance codex, the shared
        layer), utilities and companions declare their own status through the
        manifest instead of a code-level id list."""
        if tool.get("main_system_independent_tool") is not True:
            return False
        if tool.get("companion_tool") is True:
            return False
        if tool.get("hidden_from_toolbox") is True:
            return False
        return True

    @staticmethod
    def _tool_record_is_active(tool: dict[str, Any]) -> bool:
        """A533/A534: retired or disabled manifests never appear in the
        toolbox tool surface — they are non-executable lineage evidence."""
        if tool.get("enabled") is False:
            return False
        lifecycle = tool.get("lifecycle")
        return not (
            isinstance(lifecycle, dict)
            and str(lifecycle.get("status") or "").strip().casefold()
            == "retired"
        )

    def _classify_tool(self, tool: dict[str, Any]) -> bool:
        """Classify a tool record in-place. Return True if it needs process checks."""
        tool_id = str(tool.get("id", "")).strip()
        if not self._declares_independent_tool_card(tool):
            tool["hidden_from_toolbox"] = True
        if tool_id in self._RESIDENT_SERVICE_IDS:
            tool["permission_denied"] = False
            tool["lifecycle_locked"] = True
            tool["resident_service"] = True
            tool["status"] = "running"
            return False
        lifecycle = tool.get("lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
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
        def _load_and_classify() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            records = [
                tool
                for tool in self._load_manifest_records()
                if self._tool_record_is_active(tool)
            ]
            needing_checks: list[dict[str, Any]] = []
            for tool in records:
                if self._classify_tool(tool):
                    needing_checks.append(tool)
            return records, needing_checks

        # Manifest rglob + per-tool codex permission checks are synchronous;
        # keep them off the event loop so the IPC channel stays responsive.
        tools, tools_needing_checks = await asyncio.to_thread(
            _load_and_classify
        )
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

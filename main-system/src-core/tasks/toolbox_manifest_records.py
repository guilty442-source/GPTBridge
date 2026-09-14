"""Manifest record construction mixin (A185 split).

Contains the _manifest_to_record, _declared_companion_tool_directories,
and _load_manifest_records methods extracted from ManifestMixin.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class ManifestRecordMixin:
    """Manifest record construction and discovery."""

    tools_dir: Path
    project_root: Path

    def _validated_tool_directory(self, tool_dir: Path) -> Path:
        raise NotImplementedError

    def _resolve_entry_file(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        raise NotImplementedError

    def _resolve_executable_file(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        raise NotImplementedError

    def _has_governed_source_runtime(self, manifest: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def _resolve_special_unpacked_entry(
        self, manifest: Dict[str, Any], tool_dir: Path
    ) -> Path | None:
        raise NotImplementedError

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

    def _load_manifest_records(self) -> list[Dict[str, Any]]:
        records: list[Dict[str, Any]] = []
        if not self.tools_dir.exists():
            return records

        # A278/A280: independent tools live under "Standalone tools/".
        # Resident services (shared-layer, governance_rule) remain at
        # the project root.  Scan both locations.
        scan_dirs = [self.tools_dir]
        if self.project_root != self.tools_dir and self.project_root.is_dir():
            scan_dirs.append(self.project_root)

        for scan_dir in scan_dirs:
            for tool_dir in sorted(scan_dir.iterdir(), key=lambda item: item.name.lower()):
                manifest_path = tool_dir / "manifest.json"
                if not tool_dir.is_dir() or not manifest_path.exists():
                    continue
                # Skip the Standalone tools directory itself when scanning root
                if scan_dir == self.project_root and tool_dir == self.tools_dir:
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
        # Nested declared tools (e.g. local-model/model-dialogue) live inside
        # a host root; their manifest declares independence instead of a
        # code-level id list, so discover them at any depth below the hosts.
        for host_dir in sorted(self.tools_dir.iterdir(), key=lambda item: item.name.lower()):
            if not host_dir.is_dir() or not (host_dir / "manifest.json").is_file():
                continue
            for manifest_path in sorted(host_dir.rglob("manifest.json")):
                if manifest_path.parent == host_dir:
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                tool_id = str(manifest.get("id") or "")
                if not tool_id or tool_id in known_ids:
                    continue
                if manifest.get("main_system_independent_tool") is not True:
                    continue
                try:
                    records.append(self._manifest_to_record(manifest_path.parent, manifest))
                    known_ids.add(tool_id)
                except (OSError, ValueError):
                    continue
        return records


__all__ = ["ManifestRecordMixin"]

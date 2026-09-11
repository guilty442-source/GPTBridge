from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


class ToolPathResolver:
    """Resolve tool-owned paths without crossing direct-child boundaries."""

    def __init__(
        self,
        project_root: Path,
        allowed_tool_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.allowed_tool_ids = allowed_tool_ids

    @staticmethod
    def is_link_or_reparse_point(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            info = path.stat(follow_symlinks=False)
            attributes = int(getattr(info, "st_file_attributes", 0))
            reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            return bool(reparse_flag and attributes & reparse_flag)
        except OSError:
            return True

    def validated_tool_directory(self, tool_dir: Path) -> Path:
        raw_tool_dir = Path(tool_dir)
        try:
            raw_tool_dir.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ValueError("Tool directory metadata could not be verified") from error
        else:
            if self.is_link_or_reparse_point(raw_tool_dir):
                raise ValueError("Tool directory cannot be a link or reparse point")
        resolved = raw_tool_dir.resolve()
        try:
            relative = resolved.relative_to(self.project_root)
        except ValueError as error:
            raise ValueError("Tool directory escaped E:/GPTBridge") from error
        if len(relative.parts) == 1:
            return resolved
        if len(relative.parts) == 2:
            host_folder = relative.parts[0]
            host_root = self.project_root / host_folder
            if self.is_link_or_reparse_point(host_root):
                raise ValueError("Companion host directory cannot be a link or reparse point")
            manifest_path = resolved / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                host_manifest = json.loads(
                    (host_root / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError("Companion tool manifest could not be verified") from error
            host_tool_id = host_manifest.get("id")
            legacy_companion = (
                manifest.get("host_tool_id") == host_tool_id
                and manifest.get("main_system_independent_tool") is True
            )
            owned_companion = (
                manifest.get("companion_tool") is True
                and manifest.get("companion_owner") == host_tool_id
                and manifest.get("runtime_owner_tool_id") == host_tool_id
            )
            if not (legacy_companion or owned_companion):
                raise ValueError("Nested tool is not an authorized main-system companion")
            return resolved
        raise ValueError(
            "Tool directory must be a direct child or declared main-system companion"
        )

    def validated_tool_path(
        self,
        tool_dir: Path,
        candidate: Path,
        *,
        label: str,
    ) -> Path:
        tool_root = self.validated_tool_directory(tool_dir)
        lexical_root = Path(os.path.abspath(tool_dir))
        lexical_candidate = Path(os.path.abspath(candidate))
        try:
            lexical_relative = lexical_candidate.relative_to(lexical_root)
        except ValueError as error:
            raise ValueError(f"{label} escaped the tool directory") from error

        current = lexical_root
        for part in lexical_relative.parts:
            current = current / part
            try:
                current.lstat()
            except FileNotFoundError:
                break
            except OSError as error:
                raise ValueError(f"{label} metadata could not be verified") from error
            if self.is_link_or_reparse_point(current):
                raise ValueError(f"{label} traversed a link or reparse point")

        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(tool_root)
        except ValueError as error:
            raise ValueError(f"{label} escaped the tool directory") from error
        return resolved

    def resolve_entry_file(self, manifest: dict[str, Any], tool_dir: Path) -> Path:
        tool_root = self.validated_tool_directory(tool_dir)
        runtime = manifest.get("runtime")
        if isinstance(runtime, dict):
            runtime_entry = str(runtime.get("entry", "")).strip()
            if runtime_entry:
                entry_path = tool_root / Path(runtime_entry)
                if entry_path.suffix == "":
                    entry_path = entry_path.with_suffix(".py")
                return self.validated_tool_path(
                    tool_root, entry_path, label="Tool runtime entry"
                )
        entry = str(manifest.get("entry", "")).strip()
        if entry:
            entry_path = self.project_root / Path(entry)
            if entry_path.suffix == "":
                entry_path = entry_path.with_suffix(".py")
            return self.validated_tool_path(tool_root, entry_path, label="Tool entry")
        return self.validated_tool_path(
            tool_root, tool_root / "src" / "main.py", label="Tool entry"
        )

    def resolve_working_directory(
        self, manifest: dict[str, Any], tool_dir: Path
    ) -> Path:
        tool_root = self.validated_tool_directory(tool_dir)
        runtime = manifest.get("runtime")
        raw_cwd = "."
        if isinstance(runtime, dict):
            raw_cwd = str(runtime.get("workingDirectory", ".")).strip() or "."
        cwd = self.validated_tool_path(
            tool_root, tool_root / raw_cwd, label="Tool working directory"
        )
        if not cwd.is_dir():
            raise ValueError("Tool working directory does not exist")
        return cwd

    def resolve_executable_file(
        self, manifest: dict[str, Any], tool_dir: Path
    ) -> Path:
        tool_root = self.validated_tool_directory(tool_dir)
        executable = manifest.get("executable")
        raw_path = (
            str(executable.get("path", "")).strip()
            if isinstance(executable, dict)
            else ""
        )
        if not raw_path:
            tool_id = str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
            raw_path = f"dist/{tool_id}.exe"
        return self.validated_tool_path(
            tool_root, tool_root / raw_path, label="Tool executable"
        )

    def resolve_python_executable(
        self, manifest: dict[str, Any], tool_dir: Path
    ) -> Path:
        tool_root = self.validated_tool_directory(tool_dir)
        runtime = manifest.get("runtime")
        candidates: list[Path] = []
        if isinstance(runtime, dict):
            raw_python = str(runtime.get("python", "")).strip()
            if raw_python:
                python_path = Path(raw_python)
                if python_path.is_absolute():
                    resolved_python = python_path.resolve()
                    if resolved_python != Path(sys.executable).resolve():
                        raise ValueError(
                            "Absolute runtime Python must be the current interpreter"
                        )
                    candidates.append(resolved_python)
                else:
                    if self.allowed_tool_ids is not None:
                        raise ValueError(
                            "Standalone tools cannot select an unverified "
                            "tool-local Python environment"
                        )
                    candidates.append(
                        self.validated_tool_path(
                            tool_root,
                            tool_root / python_path,
                            label="Tool runtime Python",
                        )
                    )
        if self.allowed_tool_ids is None:
            if os.name == "nt":
                candidates.extend(
                    [
                        tool_root / ".venv" / "Scripts" / "pythonw.exe",
                        tool_root / ".venv" / "Scripts" / "python.exe",
                    ]
                )
            else:
                candidates.append(tool_root / ".venv" / "bin" / "python")
        candidates.append(Path(sys.executable))
        for candidate in candidates:
            if candidate.exists():
                if (
                    candidate != Path(sys.executable)
                    and self.is_link_or_reparse_point(candidate)
                ):
                    raise ValueError(
                        "Tool runtime Python cannot be a link or reparse point"
                    )
                return candidate
        return Path(sys.executable)

    @staticmethod
    def is_special_unpacked(manifest: dict[str, Any]) -> bool:
        distribution = manifest.get("distribution")
        return bool(
            isinstance(distribution, dict)
            and distribution.get("mode") == "special-unpackaged"
            and distribution.get("package") is False
        )

    @staticmethod
    def has_governed_background_source(manifest: dict[str, Any]) -> bool:
        launch = manifest.get("launch")
        request_channel = manifest.get("request_channel")
        return bool(
            isinstance(launch, dict)
            and launch.get("background") == "governed-source-channel"
            and isinstance(request_channel, dict)
            and request_channel.get("model")
            == "governance-authenticated-shared-layer"
            and request_channel.get("direct_instruction") == "PERMISSION_DENIED"
            and str(request_channel.get("runtime_entry") or "").strip()
        )

    @classmethod
    def has_governed_source_runtime(cls, manifest: dict[str, Any]) -> bool:
        return cls.is_special_unpacked(manifest) or cls.has_governed_background_source(
            manifest
        )

    @staticmethod
    def is_dual_runtime(manifest: dict[str, Any]) -> bool:
        launch = manifest.get("launch")
        if not isinstance(launch, dict):
            return False
        runtimes = launch.get("runtimes")
        if not isinstance(runtimes, list):
            return False
        normalized = {
            str(runtime).strip().casefold()
            for runtime in runtimes
            if str(runtime).strip()
        }
        return bool(
            str(launch.get("mode") or "").strip().casefold() == "dual-runtime"
            and str(launch.get("selection") or "").strip().casefold() == "automatic"
            and "executable" in normalized
            and normalized.intersection(
                {"governed-source", "governed-source-ui", "governed-source-channel"}
            )
        )

    def resolve_special_unpacked_entry(
        self, manifest: dict[str, Any], tool_dir: Path
    ) -> Path:
        if not self.has_governed_source_runtime(manifest):
            raise ValueError("Tool has no governed source runtime")
        runtime = manifest.get("runtime")
        request_channel = manifest.get("request_channel")
        if (
            not isinstance(runtime, dict)
            or runtime.get("type") != "python"
            or not isinstance(request_channel, dict)
            or request_channel.get("model")
            != "governance-authenticated-shared-layer"
            or request_channel.get("direct_instruction") != "PERMISSION_DENIED"
        ):
            raise ValueError("Special-unpackaged runtime governance is invalid")
        raw_entry = str(request_channel.get("runtime_entry") or "").strip()
        if not raw_entry or Path(raw_entry).is_absolute() or ".." in Path(raw_entry).parts:
            raise ValueError("Special-unpackaged runtime entry is invalid")
        entry = self.validated_tool_path(
            tool_dir,
            tool_dir / raw_entry,
            label="Special-unpackaged runtime entry",
        )
        if entry.suffix.lower() != ".py" or not entry.is_file():
            raise ValueError("Special-unpackaged runtime entry is missing")
        return entry


__all__ = ["ToolPathResolver"]

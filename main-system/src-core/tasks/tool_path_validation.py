"""Tool path validation mixin (A185 split).

Contains the validated_tool_directory method extracted from
ToolPathResolver.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path


class ToolPathValidationMixin:
    """Tool directory boundary validation."""

    project_root: Path

    @staticmethod
    def is_link_or_reparse_point(path: Path) -> bool:
        raise NotImplementedError

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
            # A280/A278: independent tools live under "Standalone tools/"
            # which is the canonical independent-tools-top-level-directory.
            if host_folder == "Standalone tools":
                if self.is_link_or_reparse_point(host_root):
                    raise ValueError("Standalone tools directory cannot be a link or reparse point")
                return resolved
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
        if len(relative.parts) == 3 and relative.parts[0] == "Standalone tools":
            # A278/A280: companion tools nested under an independent tool
            # inside "Standalone tools/" (e.g. model-dialogue under
            # local-model).  Validate the companion relationship.
            host_root = self.project_root / relative.parts[0] / relative.parts[1]
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
            owned_companion = (
                manifest.get("companion_tool") is True
                and manifest.get("companion_owner") == host_tool_id
            )
            # A405: a declared independent tool may be physically hosted
            # inside its owner root while remaining its own single entity
            # (e.g. model-dialogue under local-model).
            declared_independent_hosted = (
                manifest.get("main_system_independent_tool") is True
                and manifest.get("companion_tool") is not True
                and manifest.get("host_tool_id") == host_tool_id
            )
            # Independent self-hosted: model-dialogue can be physically inside
            # local-model but declare host as itself, allowing it to start
            # even when local-model is closed (design: model-dialogue opens
            # without local-model and talks to xingcheng on demand).
            independent_self_hosted = (
                manifest.get("main_system_independent_tool") is True
                and manifest.get("host_tool_id") == manifest.get("id")
                and manifest.get("id") in ("model-dialogue", "star-chat")
            )
            if not (owned_companion or declared_independent_hosted or independent_self_hosted):
                raise ValueError("Nested tool is not an authorized companion")
            return resolved
        if len(relative.parts) == 4 and relative.parts[0] == "Standalone tools":
            # A278/A280: companion tools nested under a subdirectory of an
            # independent tool inside "Standalone tools/"
            # (e.g. xingcheng under local-model/model-dialogue)
            host_root = self.project_root / relative.parts[0] / relative.parts[1]
            subdir = relative.parts[2]
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
            owned_companion = (
                manifest.get("companion_tool") is True
                and manifest.get("companion_owner") == host_tool_id
            )
            if not owned_companion:
                raise ValueError("Nested tool is not an authorized companion")
            return resolved
        raise ValueError(
            "Tool directory must be a direct child or declared main-system companion"
        )


__all__ = ["ToolPathValidationMixin"]

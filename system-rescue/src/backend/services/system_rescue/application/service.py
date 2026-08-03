from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

from ..domain.contracts import REQUIRED_PATHS, SYSTEM_RESCUE_VERSION
from ..infrastructure.audit_store import RescueRecordStore


class SystemRescueService:
    VERSION = SYSTEM_RESCUE_VERSION

    def __init__(self, project_root: Path, tool_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.tool_root = tool_root.resolve()
        self.data_root = self.tool_root / "data"
        self.records = RescueRecordStore(self.data_root)

    @staticmethod
    def _is_link(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        return stat.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400)

    def _inside_project(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.project_root)
            return True
        except (OSError, ValueError):
            return False

    def check(self) -> dict[str, Any]:
        required = []
        for relative in REQUIRED_PATHS:
            target = self.project_root / relative
            required.append(
                {
                    "path": relative,
                    "ok": target.is_file()
                    and self._inside_project(target)
                    and not self._is_link(target),
                }
            )
        nested_storage = []
        for name in ("audit", "logs"):
            root = self.data_root / name
            if root.is_dir():
                nested_storage.extend(
                    path
                    for path in root.iterdir()
                    if path.is_dir() and any(item.is_file() for item in path.rglob("*"))
                )
        issues = [item["path"] for item in required if not item["ok"]]
        if nested_storage:
            issues.append("storage-category-labels")
        result = {
            "ok": not issues,
            "operation": "system-rescue-check",
            "version": self.VERSION,
            "authority": "system-rescue",
            "required_paths": required,
            "backup_authority": "global-cleaner-via-governed-shared-layer",
            "issues": issues,
        }
        self.records.audit("check", ok=result["ok"], issues=len(issues))
        return result

    def repair(self) -> dict[str, Any]:
        """Repair managed rescue storage only; source code stays read-only."""

        removed: list[str] = []
        self.data_root.mkdir(parents=True, exist_ok=True)
        for name in ("audit", "logs"):
            root = self.data_root / name
            root.mkdir(parents=True, exist_ok=True)
            for child in tuple(root.rglob("*")):
                if child.is_file() and not self._is_link(child):
                    child.unlink()
                    removed.append(str(child.relative_to(self.tool_root)))
        result = {
            "ok": True,
            "operation": "system-rescue-owned-storage-cleanup",
            "authority": "system-rescue",
            "source_modified": False,
            "directories_preserved": True,
            "removed": removed,
        }
        self.records.audit("repair", ok=True, removed=len(removed))
        return result

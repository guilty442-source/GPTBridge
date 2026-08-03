from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from ..infrastructure.cleanup_engine import ProjectCleanupService as CleanupEngine


class ProjectCleanupService(CleanupEngine):
    """Governed application operations layered over the cleanup engine."""

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        metadata = path.lstat()
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        return stat.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400)

    def clear_managed_temp_files(self, relative_root: str) -> dict[str, Any]:
        with self._mutation_guard("managed-temp-cleanup") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": bool(lock.get("busy")),
                    "operation": "clear-managed-temp-files",
                    "target": str(relative_root or ""),
                    "removed_files": 0,
                    "removed_bytes": 0,
                    "skipped": [],
                    "message": str(
                        lock.get("message") or "project cleaner is busy"
                    ),
                }
            return self._clear_managed_temp_files_unlocked(relative_root)

    def _clear_managed_temp_files_unlocked(
        self,
        relative_root: str,
    ) -> dict[str, Any]:
        requester = str(
            os.environ.get("GPTBRIDGE_GOVERNED_REQUESTER_ACTOR") or ""
        ).strip()
        if requester not in {
            "governance/main-system",
            "governance/tool/global-cleaner",
        }:
            raise PermissionError("PERMISSION_DENIED")

        relative = Path(str(relative_root or "").replace("\\", "/"))
        parts = relative.parts
        is_managed_temp_root = (
            len(parts) >= 3
            and parts[:3] == ("global-cleaner", "runtime", "temp")
        )
        if (
            relative.is_absolute()
            or not is_managed_temp_root
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise PermissionError("PERMISSION_DENIED")
        target = (self.project_root / relative).resolve()
        expected_temp = (
            self.project_root / "global-cleaner" / "runtime" / "temp"
        ).resolve()
        cleaner_manifest = (
            self.project_root / "global-cleaner" / "manifest.json"
        )
        try:
            target.relative_to(expected_temp)
        except ValueError as error:
            raise PermissionError("PERMISSION_DENIED") from error
        if (
            not cleaner_manifest.is_file()
            or not target.is_dir()
            or self._is_link_or_reparse(target)
        ):
            raise PermissionError("PERMISSION_DENIED")

        removed_files = 0
        removed_bytes = 0
        skipped: list[str] = []
        for candidate in sorted(target.rglob("*")):
            try:
                if self._is_link_or_reparse(candidate):
                    skipped.append(candidate.relative_to(self.project_root).as_posix())
                    continue
                if candidate.is_dir():
                    continue
                if not candidate.is_file():
                    skipped.append(candidate.relative_to(self.project_root).as_posix())
                    continue
                size = candidate.stat().st_size
                candidate.unlink()
                removed_files += 1
                removed_bytes += size
            except OSError:
                skipped.append(candidate.relative_to(self.project_root).as_posix())
        result = {
            "ok": not skipped,
            "operation": "clear-managed-temp-files",
            "authority": "global-cleaner",
            "target": relative.as_posix(),
            "removed_files": removed_files,
            "removed_bytes": removed_bytes,
            "directories_preserved": True,
            "skipped": skipped,
        }
        self._append_history(
            "clear-managed-temp-files",
            ok=result["ok"],
            scope=relative.as_posix(),
            item_count=removed_files,
            bytes=removed_bytes,
            errors=len(skipped),
        )
        return result


__all__ = ["ProjectCleanupService"]

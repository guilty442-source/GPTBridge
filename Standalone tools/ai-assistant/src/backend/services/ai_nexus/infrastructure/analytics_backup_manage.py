from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .analytics_common import _validated_storage_path


class BackupManageMixin:
    """Backup listing and retention pruning."""

    def list_backups(self) -> list[dict[str, Any]]:
        output = []
        for path in sorted(self.backup_root.glob("*.ivault"), reverse=True)[:100]:
            manifest_path = path.with_name(
                f"{path.stem}.manifest.json"
            )
            manifest: dict[str, Any] = {}
            integrity = None
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    declared = next(
                        (
                            item
                            for item in manifest.get("files", [])
                            if isinstance(item, dict) and item.get("role") == "analytics_database"
                        ),
                        {},
                    )
                    integrity = (
                        int(declared.get("size") or -1) == path.stat().st_size
                        and str(declared.get("sha256") or "") == hashlib.sha256(path.read_bytes()).hexdigest()
                    )
                except (OSError, ValueError, json.JSONDecodeError):
                    integrity = False
            output.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size": path.stat().st_size,
                    "modified_at": datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).isoformat(),
                    "backup_id": str(manifest.get("backup_id") or ""),
                    "manifest_path": str(manifest_path) if manifest_path.exists() else "",
                    "integrity_verified": integrity,
                    "state_included": any(
                        isinstance(item, dict) and item.get("role") == "portfolio_state"
                        for item in manifest.get("files", [])
                    ),
                }
            )
        return output

    def prune_backups(
        self,
        *,
        max_total: int = 1,
        max_automatic: int = 1,
        remove_all: bool = False,
    ) -> dict[str, int]:
        # Retention is global across every published backup type. Arguments
        # remain for API compatibility but cannot change the one-copy limit.
        del max_total, max_automatic
        retention_root = self._backup_retention_root()
        paths = sorted(
            (
                path
                for pattern in ("*.ivault", "*.zip")
                for path in retention_root.rglob(pattern)
                if path.is_file()
            ),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )

        retained_limit = 0 if remove_all else 1
        to_delete = paths[retained_limit:]
        removed, deleted_bytes = self._delete_backup_generations(
            to_delete,
            retention_root,
        )

        retained = sum(
            1
            for pattern in ("*.ivault", "*.zip")
            for path in retention_root.rglob(pattern)
            if path.is_file()
        )

        return {
            "retained": retained,
            "removed": removed,
            "deleted_bytes": deleted_bytes,
            "over_total_limit": max(0, retained - retained_limit),
            "over_automatic_limit": 0,
        }

    def _backup_retention_root(self) -> Path:
        managed_storage = str(
            os.environ.get("GPTBRIDGE_MANAGED_STORAGE_ROOT") or ""
        ).strip()
        retention_root = (
            self.managed_storage_root / "backups"
            if managed_storage
            else self.backup_root
        )
        return _validated_storage_path(
            retention_root,
            label="Global managed backup root",
            boundary=self.managed_storage_root,
            require_exists=True,
            expected_kind="directory",
        )

    @staticmethod
    def _delete_backup_generations(
        to_delete: Sequence[Path],
        retention_root: Path,
    ) -> tuple[int, int]:
        removed = 0
        deleted_bytes = 0
        for path in to_delete:
            path = _validated_storage_path(
                path,
                label="Published backup generation",
                boundary=retention_root,
                require_exists=True,
                expected_kind="file",
            )
            sidecars = () if path.suffix.lower() == ".zip" else (
                path.with_name(f"{path.stem}.manifest.json"),
                path.with_name(f"{path.stem}.statevault"),
            )
            try:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                path.unlink()
                removed += 1
                deleted_bytes += size
            except OSError:
                # If a file cannot be unlinked, skip it and continue with others
                continue
            for sidecar in sidecars:
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass
        return removed, deleted_bytes

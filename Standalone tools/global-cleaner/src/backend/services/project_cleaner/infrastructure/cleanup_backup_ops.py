from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any

from .cleanup_constants import MANAGED_BACKUP_RETENTION_PER_OWNER


class BackupOpsMixin:
    """Managed backup extraction and governed daily maintenance."""

    def extract_managed_backup(
        self,
        owner_id: str,
        requested_paths: list[str],
    ) -> dict[str, Any]:
        with self._mutation_guard("managed-backup-extract") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": bool(lock.get("busy")),
                    "operation": "managed-backup-extract",
                    "owner_id": str(owner_id or ""),
                    "error_code": "CLEANER_BUSY",
                    "message": str(
                        lock.get("message") or "project cleaner is busy"
                    ),
                }
            return self._extract_managed_backup_unlocked(
                owner_id,
                requested_paths,
            )

    def _extract_managed_backup_unlocked(
        self,
        owner_id: str,
        requested_paths: list[str],
    ) -> dict[str, Any]:
        owner_id, _source_root = self._validated_backup_owner(owner_id)
        requester = self._authorized_backup_requester(owner_id, extraction=True)
        request_id = str(
            os.environ.get("GPTBRIDGE_GOVERNED_REQUEST_ID") or ""
        ).strip()
        if (
            not request_id
            or len(request_id) > 128
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-" for character in request_id)
        ):
            raise PermissionError("PERMISSION_DENIED")
        archives = sorted(
            (self.backup_root / owner_id).glob("*.zip"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        if not archives:
            return {
                "ok": False,
                "operation": "managed-backup-extract",
                "owner_id": owner_id,
                "error_code": "BACKUP_NOT_FOUND",
                "message": "managed backup is unavailable",
            }
        archive_path = archives[0]
        verified, _file_count, verification_error = self._verify_published_backup(
            owner_id,
            archive_path,
        )
        if not verified:
            return {
                "ok": False,
                "operation": "managed-backup-extract",
                "owner_id": owner_id,
                "error_code": "BACKUP_INTEGRITY_FAILED",
                "message": verification_error or "backup integrity verification failed",
            }
        normalized_paths: set[str] = set()
        owner_prefix = f"{owner_id}/"
        for raw_path in requested_paths:
            normalized = Path(str(raw_path or "").replace("\\", "/")).as_posix().lstrip("/")
            if (
                not normalized
                or normalized.startswith("../")
                or "/../" in f"/{normalized}/"
            ):
                raise PermissionError("PERMISSION_DENIED")
            if not normalized.startswith(owner_prefix):
                normalized = f"{owner_prefix}{normalized}"
            normalized_paths.add(normalized)
        if not normalized_paths or len(normalized_paths) > 256:
            raise PermissionError("PERMISSION_DENIED")
        extraction_root = (self.backup_extract_root / request_id).resolve()
        extraction_root.relative_to(self.backup_extract_root.resolve())
        extraction_root.mkdir(parents=True, exist_ok=False)
        extracted: list[dict[str, Any]] = []
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                available = set(archive.namelist())
                for relative in sorted(normalized_paths):
                    if relative not in available or relative.endswith("/"):
                        continue
                    destination = (extraction_root / relative).resolve()
                    destination.relative_to(extraction_root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(relative, "r") as source, destination.open("xb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    extracted.append(
                        {
                            "path": relative,
                            "staged_path": str(destination),
                            "size_bytes": destination.stat().st_size,
                            "sha256": self._file_sha256(destination),
                        }
                    )
            evidence = {
                "schema_version": 1,
                "operation": "governed-backup-extract",
                "request_id": request_id,
                "requester_actor": requester,
                "owner_id": owner_id,
                "backup": archive_path.name,
                "created_at": self._iso_now(),
                "items": extracted,
                "apply_authority": False,
            }
            self._atomic_write_json(extraction_root / "extraction-manifest.json", evidence)
        except Exception:
            for candidate in extraction_root.rglob("*"):
                if candidate.is_file() and not self._is_link_or_reparse_point(candidate):
                    candidate.unlink(missing_ok=True)
            raise
        return {
            "ok": bool(extracted),
            "operation": "managed-backup-extract",
            "authority": "global-cleaner",
            "owner_id": owner_id,
            "requester_actor": requester,
            "request_id": request_id,
            "extraction_root": str(extraction_root),
            "manifest": str(extraction_root / "extraction-manifest.json"),
            "items": extracted,
            "apply_authority": False,
            "message": (
                "governed backup extraction staged for separately authorized repair"
                if extracted
                else "requested recovery paths were not present in the backup"
            ),
        }

    def run_governed_daily_maintenance(self) -> dict[str, Any]:
        requester = str(
            os.environ.get("GPTBRIDGE_GOVERNED_REQUESTER_ACTOR") or ""
        ).strip()
        if requester != "governance/main-system":
            raise PermissionError("PERMISSION_DENIED")
        # Cross-module low-risk garbage cleanup has been devolved to each
        # independent tool's own local self-cleanup at boot. The central daily
        # maintenance now only owns governed backups, which remain the single
        # backup authority that recovery/system-rescue depend on.
        backups: list[dict[str, Any]] = []
        for owner_id in sorted(self._registered_backup_owners()):
            self._emit_progress(
                "backup",
                0,
                "建立治理備份",
                current_path=owner_id,
            )
            backups.append(self.create_managed_backup(owner_id))
        failed_backups = [item for item in backups if item.get("ok") is not True]
        cleanup = {
            "ok": True,
            "operation": "local-self-cleanup-devolved",
            "cleaned_files": 0,
            "cleaned_dirs": 0,
            "cleaned_bytes": 0,
            "message": "garbage cleanup devolved to per-tool local self-cleanup",
        }
        result = {
            "ok": not failed_backups,
            "operation": "governed-daily-maintenance",
            "authority": "governance/main-system -> shared-layer -> global-cleaner",
            "cleanup": cleanup,
            "backups": backups,
            "backup_owner_count": len(backups),
            "backup_failure_count": len(failed_backups),
            "message": "daily governed backup completed; garbage cleanup devolved to per-tool self-cleanup",
        }
        self._append_history(
            "daily-maintenance",
            ok=result["ok"],
            scope="global",
            item_count=0,
            bytes=0,
            errors=len(failed_backups),
        )
        return result

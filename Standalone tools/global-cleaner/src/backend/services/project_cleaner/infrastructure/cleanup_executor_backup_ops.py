# Managed-backup extraction and verification mixin for the cleaner executor.
from __future__ import annotations

import hmac
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any

from .cleanup_helpers import MANAGED_BACKUP_SCHEMA_VERSION


class CleanupBackupOpsMixin:

        @staticmethod
        def _verify_backup_archive(path: Path) -> tuple[bool, int, str]:
            try:
                with zipfile.ZipFile(path, "r") as archive:
                    invalid = archive.testzip()
                    names = archive.namelist()
                    if invalid is not None or "gptbridge-managed-backup.json" not in names:
                        return False, len(names), invalid or "metadata-missing"
                    return True, max(0, len(names) - 1), ""
            except (OSError, zipfile.BadZipFile, RuntimeError) as error:
                return False, 0, f"{type(error).__name__}: {error}"

        def _verify_published_backup(
            self,
            owner_id: str,
            archive_path: Path,
        ) -> tuple[bool, int, str]:
            verified, file_count, error = self._verify_backup_archive(archive_path)
            if not verified:
                return False, file_count, error
            manifest_path = archive_path.with_name(
                f"{archive_path.stem}.manifest.json"
            )
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError("backup manifest is not an object")
                if not self._published_manifest_valid(
                    manifest, owner_id, archive_path, file_count
                ):
                    return False, file_count, "published backup manifest mismatch"
                with zipfile.ZipFile(archive_path, "r") as archive:
                    embedded = json.loads(
                        archive.read("gptbridge-managed-backup.json").decode("utf-8")
                    )
                if (
                    not isinstance(embedded, dict)
                    or embedded.get("schema_version")
                    != MANAGED_BACKUP_SCHEMA_VERSION
                    or embedded.get("owner_id") != owner_id
                ):
                    return False, file_count, "embedded backup identity mismatch"
            except (
                KeyError,
                OSError,
                UnicodeError,
                ValueError,
                json.JSONDecodeError,
                zipfile.BadZipFile,
            ) as verification_error:
                return (
                    False,
                    file_count,
                    f"{type(verification_error).__name__}: {verification_error}",
                )
            return True, file_count, ""

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
            request_id = self._extraction_request_id()
            archive_path, failure = self._latest_verified_backup(owner_id)
            if failure is not None:
                return failure
            normalized_paths = self._normalized_extract_paths(
                owner_id, requested_paths
            )
            extraction_root = (self.backup_extract_root / request_id).resolve()
            extraction_root.relative_to(self.backup_extract_root.resolve())
            extraction_root.mkdir(parents=True, exist_ok=False)
            try:
                extracted = self._extract_backup_members(
                    archive_path, extraction_root, normalized_paths
                )
                self._write_extraction_evidence(
                    extraction_root, request_id, requester,
                    owner_id, archive_path, extracted,
                )
            except Exception:
                self._cleanup_failed_extraction(extraction_root)
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

        @staticmethod
        def _extraction_request_id() -> str:
            request_id = str(
                os.environ.get("GPTBRIDGE_GOVERNED_REQUEST_ID") or ""
            ).strip()
            if (
                not request_id
                or len(request_id) > 128
                or any(
                    character
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-"
                    for character in request_id
                )
            ):
                raise PermissionError("PERMISSION_DENIED")
            return request_id

        @staticmethod
        def _normalized_extract_paths(
            owner_id: str,
            requested_paths: list[str],
        ) -> set[str]:
            normalized_paths: set[str] = set()
            owner_prefix = f"{owner_id}/"
            for raw_path in requested_paths:
                normalized = (
                    Path(str(raw_path or "").replace("\\", "/"))
                    .as_posix()
                    .lstrip("/")
                )
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
            return normalized_paths

        def _extract_backup_members(
            self,
            archive_path: Path,
            extraction_root: Path,
            normalized_paths: set[str],
        ) -> list[dict[str, Any]]:
            extracted: list[dict[str, Any]] = []
            with zipfile.ZipFile(archive_path, "r") as archive:
                available = set(archive.namelist())
                for relative in sorted(normalized_paths):
                    if relative not in available or relative.endswith("/"):
                        continue
                    destination = (extraction_root / relative).resolve()
                    destination.relative_to(extraction_root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        archive.open(relative, "r") as source,
                        destination.open("xb") as target,
                    ):
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    extracted.append(
                        {
                            "path": relative,
                            "staged_path": str(destination),
                            "size_bytes": destination.stat().st_size,
                            "sha256": self._file_sha256(destination),
                        }
                    )
            return extracted

        def _published_manifest_valid(
            self,
            manifest: dict[str, Any],
            owner_id: str,
            archive_path: Path,
            file_count: int,
        ) -> bool:
            expected_size = manifest.get("archive_size")
            expected_count = manifest.get("source_file_count")
            expected_digest = manifest.get("archive_sha256")
            return (
                manifest.get("schema_version") == MANAGED_BACKUP_SCHEMA_VERSION
                and manifest.get("owner_id") == owner_id
                and manifest.get("archive") == archive_path.name
                and manifest.get("integrity_verified") is True
                and isinstance(expected_size, int)
                and not isinstance(expected_size, bool)
                and isinstance(expected_count, int)
                and not isinstance(expected_count, bool)
                and isinstance(expected_digest, str)
                and len(expected_digest) == 64
                and archive_path.stat().st_size == expected_size
                and file_count == expected_count
                and hmac.compare_digest(
                    self._file_sha256(archive_path),
                    expected_digest,
                )
            )

        def _latest_verified_backup(
            self, owner_id: str
        ) -> tuple[Path | None, dict[str, Any] | None]:
            archives = sorted(
                (self.backup_root / owner_id).glob("*.zip"),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
            if not archives:
                return None, {
                    "ok": False,
                    "operation": "managed-backup-extract",
                    "owner_id": owner_id,
                    "error_code": "BACKUP_NOT_FOUND",
                    "message": "managed backup is unavailable",
                }
            archive_path = archives[0]
            verified, _file_count, verification_error = (
                self._verify_published_backup(owner_id, archive_path)
            )
            if not verified:
                return None, {
                    "ok": False,
                    "operation": "managed-backup-extract",
                    "owner_id": owner_id,
                    "error_code": "BACKUP_INTEGRITY_FAILED",
                    "message": (
                        verification_error
                        or "backup integrity verification failed"
                    ),
                }
            return archive_path, None

        def _write_extraction_evidence(
            self,
            extraction_root: Path,
            request_id: str,
            requester: str,
            owner_id: str,
            archive_path: Path,
            extracted: list[dict[str, Any]],
        ) -> None:
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
            self._atomic_write_json(
                extraction_root / "extraction-manifest.json", evidence
            )

        def _cleanup_failed_extraction(self, extraction_root: Path) -> None:
            for candidate in extraction_root.rglob("*"):
                if candidate.is_file() and not self._is_link_or_reparse_point(candidate):
                    candidate.unlink(missing_ok=True)

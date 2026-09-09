from __future__ import annotations

import fnmatch
import hmac
import json
import os
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .cleanup_constants import MANAGED_BACKUP_RETENTION_PER_OWNER, MANAGED_BACKUP_SCHEMA_VERSION


class BackupMixin:
    """Managed backup creation, listing, and verification."""

    def _registered_backup_owners(self) -> dict[str, Path]:
        owners = {"main-system": (self.project_root / "main-system").resolve()}
        for manifest_path in self.project_root.glob("*/manifest.json"):
            try:
                document = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            tool_id = str(document.get("id") or "").strip()
            backup_owner = str(document.get("backup_owner") or "").strip()
            if (
                not tool_id
                or (backup_owner and backup_owner != tool_id)
                or tool_id in {"governance-rule", "governance_rule"}
                or not fnmatch.fnmatchcase(tool_id, "[a-z0-9]*")
                or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in tool_id)
            ):
                continue
            tool_root = manifest_path.parent.resolve()
            if tool_root.parent == self.project_root:
                owners[tool_id] = tool_root
        return owners

    def _authorized_backup_requester(
        self,
        owner_id: str,
        *,
        extraction: bool = False,
    ) -> str:
        actor = str(
            os.environ.get("GPTBRIDGE_GOVERNED_REQUESTER_ACTOR") or ""
        ).strip()
        allowed = {
            "governance/main-system",
            "governance/tool/global-cleaner",
            f"governance/tool/{owner_id}",
        }
        if extraction:
            allowed = {
                "governance/main-system",
                "governance/tool/global-cleaner",
                "governance/tool/system-rescue",
            }
        if actor not in allowed:
            raise PermissionError("PERMISSION_DENIED")
        return actor

    def _validated_backup_owner(self, owner_id: str) -> tuple[str, Path]:
        normalized = str(owner_id or "").strip()
        owner_root = self._registered_backup_owners().get(normalized)
        if owner_root is None:
            raise PermissionError("PERMISSION_DENIED")
        owner_root.relative_to(self.project_root)
        if self._is_link_or_reparse_point(owner_root):
            raise PermissionError("PERMISSION_DENIED")
        return normalized, owner_root

    def _backup_source_files(self, owner_root: Path) -> Iterator[tuple[Path, str]]:
        excluded_names = {
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "node_modules",
            "dist",
            "dist-ui",
            "release",
            "browser-profile",
            "browser-profiles",
            "edge-profile",
            "electron-user-data",
            "backups",
            "cache",
            "temp",
            "tmp",
        }
        backup_root = self.backup_root.resolve()
        extract_root = self.backup_extract_root.resolve()
        for current_raw, directory_names, file_names in os.walk(
            owner_root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_raw)
            kept_directories: list[str] = []
            for name in directory_names:
                candidate = current / name
                if name.casefold() in excluded_names or self._is_link_or_reparse_point(candidate):
                    continue
                resolved = candidate.resolve()
                if resolved == backup_root or backup_root in resolved.parents:
                    continue
                if resolved == extract_root or extract_root in resolved.parents:
                    continue
                kept_directories.append(name)
            directory_names[:] = kept_directories
            for name in file_names:
                source = current / name
                if self._is_link_or_reparse_point(source) or not source.is_file():
                    continue
                resolved = source.resolve()
                try:
                    resolved.relative_to(owner_root)
                except ValueError:
                    continue
                yield source, resolved.relative_to(self.project_root).as_posix()

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
            expected_size = manifest.get("archive_size")
            expected_count = manifest.get("source_file_count")
            expected_digest = manifest.get("archive_sha256")
            if (
                manifest.get("schema_version") != MANAGED_BACKUP_SCHEMA_VERSION
                or manifest.get("owner_id") != owner_id
                or manifest.get("archive") != archive_path.name
                or manifest.get("integrity_verified") is not True
                or not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or not isinstance(expected_count, int)
                or isinstance(expected_count, bool)
                or not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or archive_path.stat().st_size != expected_size
                or file_count != expected_count
                or not hmac.compare_digest(
                    self._file_sha256(archive_path),
                    expected_digest,
                )
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

    def _prune_owner_backups(self, owner_root: Path, keep_stem: str) -> dict[str, Any]:
        removed: list[str] = []
        errors: list[dict[str, str]] = []
        generations = sorted(
            owner_root.glob("*.zip"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        retained_stems = {keep_stem}
        for archive in generations:
            if archive.stem in retained_stems:
                continue
            if len(retained_stems) < MANAGED_BACKUP_RETENTION_PER_OWNER:
                retained_stems.add(archive.stem)
                continue
            for candidate in (
                archive,
                archive.with_name(f"{archive.stem}.manifest.json"),
            ):
                try:
                    candidate.unlink(missing_ok=True)
                    if candidate == archive:
                        removed.append(candidate.name)
                except OSError as error:
                    errors.append(
                        {"path": str(candidate), "message": str(error)}
                    )
        return {"removed": removed, "errors": errors}

    def _retire_legacy_main_backups(self) -> dict[str, Any]:
        legacy_root = (self.project_root / "system-rescue" / "data" / "backups").resolve()
        expected_root = (self.project_root / "system-rescue" / "data" / "backups").resolve()
        removed: list[str] = []
        errors: list[dict[str, str]] = []
        if legacy_root != expected_root or not legacy_root.is_dir():
            return {"removed": removed, "errors": errors}
        if self._is_link_or_reparse_point(legacy_root):
            return {
                "removed": removed,
                "errors": [{"path": str(legacy_root), "message": "legacy root is a link"}],
            }
        for candidate in legacy_root.iterdir():
            if not candidate.is_file() or self._is_link_or_reparse_point(candidate):
                continue
            lowered_name = candidate.name.casefold()
            if not (
                lowered_name.endswith(".zip")
                or lowered_name.endswith(".manifest.json")
            ):
                continue
            try:
                candidate.resolve().relative_to(legacy_root)
                candidate.unlink()
                removed.append(candidate.name)
            except (OSError, ValueError) as error:
                errors.append({"path": str(candidate), "message": str(error)})
        return {"removed": removed, "errors": errors}

    def create_managed_backup(self, owner_id: str) -> dict[str, Any]:
        with self._mutation_guard("managed-backup-create") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": bool(lock.get("busy")),
                    "operation": "managed-backup-create",
                    "owner_id": str(owner_id or ""),
                    "error_code": "CLEANER_BUSY",
                    "message": str(
                        lock.get("message") or "project cleaner is busy"
                    ),
                }
            return self._create_managed_backup_unlocked(owner_id)

    def _create_managed_backup_unlocked(self, owner_id: str) -> dict[str, Any]:
        owner_id, source_root = self._validated_backup_owner(owner_id)
        requester = self._authorized_backup_requester(owner_id)
        owner_backup_root = (self.backup_root / owner_id).resolve()
        owner_backup_root.relative_to(self.backup_root.resolve())
        owner_backup_root.mkdir(parents=True, exist_ok=True)
        temp_root = self.project_root / "global-cleaner" / "runtime" / "temp" / "backups"
        temp_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        stem = f"{stamp}-{uuid.uuid4().hex[:8]}"
        temporary_path = temp_root / f"{owner_id}-{stem}.zip.tmp"
        final_path = owner_backup_root / f"{stem}.zip"
        manifest_path = owner_backup_root / f"{stem}.manifest.json"
        file_count = 0
        total_source_bytes = 0
        try:
            with zipfile.ZipFile(
                temporary_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=True,
            ) as archive:
                for source, relative in self._backup_source_files(source_root):
                    before = source.stat()
                    archive.write(source, relative)
                    after = source.stat()
                    if (
                        before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                    ):
                        raise RuntimeError(
                            f"backup source changed during capture: {relative}"
                        )
                    file_count += 1
                    total_source_bytes += after.st_size
                archive.writestr(
                    "gptbridge-managed-backup.json",
                    json.dumps(
                        {
                            "schema_version": MANAGED_BACKUP_SCHEMA_VERSION,
                            "version": self.VERSION,
                            "owner_id": owner_id,
                            "created_at": self._iso_now(),
                            "requester_actor": requester,
                            "retention_per_owner": MANAGED_BACKUP_RETENTION_PER_OWNER,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            verified, verified_files, verification_error = self._verify_backup_archive(temporary_path)
            if not verified or verified_files != file_count:
                raise RuntimeError(
                    verification_error or "backup file-count verification failed"
                )
            archive_size = temporary_path.stat().st_size
            archive_sha256 = self._file_sha256(temporary_path)
            os.replace(temporary_path, final_path)
            manifest = {
                "schema_version": MANAGED_BACKUP_SCHEMA_VERSION,
                "version": self.VERSION,
                "owner_id": owner_id,
                "created_at": self._iso_now(),
                "archive": final_path.name,
                "archive_size": archive_size,
                "archive_sha256": archive_sha256,
                "source_file_count": file_count,
                "source_bytes": total_source_bytes,
                "integrity_verified": True,
            }
            self._atomic_write_json(manifest_path, manifest)
            published, published_files, published_error = (
                self._verify_published_backup(owner_id, final_path)
            )
            if not published or published_files != file_count:
                raise RuntimeError(
                    published_error
                    or "published backup integrity verification failed"
                )
        except Exception as error:
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            return {
                "ok": False,
                "operation": "managed-backup-create",
                "owner_id": owner_id,
                "error_code": "BACKUP_CREATE_FAILED",
                "message": f"{type(error).__name__}: {error}",
            }
        retention = self._prune_owner_backups(owner_backup_root, final_path.stem)
        legacy_retirement = (
            self._retire_legacy_main_backups()
            if owner_id == "main-system"
            else {"removed": [], "errors": []}
        )
        retention["errors"].extend(legacy_retirement["errors"])
        backup_count = len(tuple(owner_backup_root.glob("*.zip")))
        result = {
            "ok": not retention["errors"] and backup_count <= MANAGED_BACKUP_RETENTION_PER_OWNER,
            "operation": "managed-backup-create",
            "authority": "global-cleaner",
            "requester_actor": requester,
            "owner_id": owner_id,
            "backup": str(final_path),
            "manifest": str(manifest_path),
            "file_count": file_count,
            "source_bytes": total_source_bytes,
            "integrity_verified": True,
            "backup_count": backup_count,
            "backup_limit": MANAGED_BACKUP_RETENTION_PER_OWNER,
            "removed_excess": retention["removed"],
            "removed_legacy_excess": legacy_retirement["removed"],
            "errors": retention["errors"],
            "message": "managed backup created and owner retention enforced",
        }
        self._append_history(
            "managed-backup",
            ok=result["ok"],
            scope=owner_id,
            item_count=file_count,
            bytes=final_path.stat().st_size,
            errors=len(retention["errors"]),
        )
        return result

    def list_managed_backups(self) -> dict[str, Any]:
        owners: dict[str, list[dict[str, Any]]] = {}
        for owner_id in sorted(self._registered_backup_owners()):
            owner_root = self.backup_root / owner_id
            rows = []
            if owner_root.is_dir() and not self._is_link_or_reparse_point(owner_root):
                for archive in sorted(owner_root.glob("*.zip"), reverse=True):
                    verified, file_count, error = self._verify_published_backup(
                        owner_id,
                        archive,
                    )
                    rows.append(
                        {
                            "name": archive.name,
                            "size_bytes": archive.stat().st_size,
                            "integrity_verified": verified,
                            "file_count": file_count,
                            "error": error,
                        }
                    )
            owners[owner_id] = rows
        return {
            "ok": all(
                len(rows) <= MANAGED_BACKUP_RETENTION_PER_OWNER
                and all(row.get("integrity_verified") is True for row in rows)
                for rows in owners.values()
            ),
            "operation": "managed-backup-list",
            "authority": "global-cleaner",
            "retention_per_owner": MANAGED_BACKUP_RETENTION_PER_OWNER,
            "owners": owners,
        }

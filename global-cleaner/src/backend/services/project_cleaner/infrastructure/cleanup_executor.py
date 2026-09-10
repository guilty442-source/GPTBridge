# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import stat as stat_module
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request

from .business_history import BusinessHistoryStore

try:
    from governance_rule.execution.git_tiers import audit_log, enforce
except Exception:
    audit_log = None
    enforce = None

from .cleanup_helpers import SECONDS_PER_DAY, LEGACY_QUARANTINE_ROOT_NAME, LEGACY_RECOVERY_ROOT_NAME, QUARANTINE_RELATIVE_PATH, RECOVERY_RELATIVE_PATH, CLEANER_RUNTIME_RELATIVE_PATH, DEFAULT_QUARANTINE_TTL_HOURS, DEFAULT_PLAN_TTL_MINUTES, MAX_REPORTED_SKIPS, MAX_HISTORY_RECORDS, PROGRESS_JSON_PREFIX, MANAGED_BACKUP_SCHEMA_VERSION, MANAGED_BACKUP_RETENTION_PER_OWNER, MANAGED_BACKUP_RELATIVE_ROOT, BACKUP_EXTRACT_RELATIVE_ROOT, SYSTEM_RESCUE_REQUIRED_PATHS, SYSTEM_RESCUE_REPAIR_ANOMALIES, PLAN_SCHEMA_VERSION, QUARANTINE_SCHEMA_VERSION, CORE_EXCLUDED_DIRECTORY_NAMES, CORE_PROTECTED_RELATIVE_PATHS, SOURCE_LIKE_SUFFIXES, _PROCESS_LOCKS_GUARD, _PROCESS_LOCKS, FALLBACK_RULES, ProgressCallback, _background_subprocess_kwargs, _deep_merge, _parse_iso


class CleanupExecutorMixin:
    def purge_legacy_artifacts(self, *, force: bool = False) -> dict[str, Any]:
        """Delete only declared obsolete package and root-level runtime artifacts.

        This explicit maintenance operation is intentionally separate from
        ordinary cleanup policy. It never follows links, never crosses the
        project root, never removes a registered tool's current ``dist``, and
        refuses Git-tracked content. ``force`` is required because deletion is
        permanent and is used only for an explicitly authorized refactor.
        """

        if not force:
            return {
                "ok": False,
                "error_code": "CONFIRMATION_REQUIRED",
                "message": "force confirmation is required",
            }
        with self._mutation_guard("legacy-artifact-purge") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": bool(lock.get("busy")),
                    "error_code": "CLEANER_BUSY",
                    "message": str(
                        lock.get("message") or "project cleaner is busy"
                    ),
                }
            return self._purge_legacy_artifacts_unlocked()


    def _purge_legacy_artifacts_unlocked(self) -> dict[str, Any]:
        candidates: set[Path] = set()
        for name in (
            "backups",
            "release",
            "tmp",
            "test-results",
            ".pytest_cache",
            LEGACY_QUARANTINE_ROOT_NAME,
            LEGACY_RECOVERY_ROOT_NAME,
        ):
            candidates.add(self.project_root / name)

        tools_root = self.project_root / "platform_tools"
        if tools_root.is_dir() and not self._is_link_or_reparse_point(tools_root):
            for tool_dir in tools_root.iterdir():
                if not tool_dir.is_dir() or self._is_link_or_reparse_point(tool_dir):
                    continue
                if not (tool_dir / "manifest.json").is_file():
                    candidates.add(tool_dir)
                else:
                    candidates.add(tool_dir / "build")

        # Keep only outermost candidates so a parent deletion owns its nested
        # backups and no child is evaluated after its parent is gone.
        ordered: list[Path] = []
        for candidate in sorted(candidates, key=lambda item: len(item.parts)):
            resolved = self._safe_resolve(candidate)
            if any(
                resolved == parent or parent in resolved.parents
                for parent in ordered
            ):
                continue
            ordered.append(resolved)

        removed: list[str] = []
        skipped: list[dict[str, str]] = []
        removed_bytes = 0
        for target in ordered:
            try:
                target.relative_to(self.project_root)
                if (
                    target == self.project_root
                    or not target.exists()
                    or self._is_link_or_reparse_point(target)
                ):
                    continue
                item_type = "directory" if target.is_dir() else "file"
                protection = self._git_protection_reason(target, item_type)
                if protection == "directory contains git-tracked files":
                    tracked, _status = self._git_snapshot()
                    prefix = self._relative_path(target).rstrip("/") + "/"
                    if not any(
                        item.startswith(prefix)
                        and (self.project_root / Path(item)).exists()
                        for item in tracked
                    ):
                        protection = ""
                if protection:
                    skipped.append(
                        {"path": self._relative_path(target), "reason": protection}
                    )
                    continue
                snapshot = self._candidate_snapshot(target, item_type)
                removed_bytes += int(snapshot.get("size_bytes") or 0)
                relative = self._relative_path(target)
                if item_type == "directory":
                    def clear_readonly_and_retry(
                        operation: Callable[..., Any],
                        path: str,
                        _error: tuple[type[BaseException], BaseException, Any],
                    ) -> None:
                        os.chmod(path, stat_module.S_IWRITE)
                        operation(path)

                    shutil.rmtree(target, onerror=clear_readonly_and_retry)
                else:
                    target.unlink()
                removed.append(relative)
            except (OSError, ValueError) as error:
                skipped.append(
                    {"path": str(target), "reason": f"{type(error).__name__}: {error}"}
                )
        return {
            "ok": not skipped,
            "authority": "global-cleaner",
            "boundary": "project-only",
            "removed": removed,
            "removed_count": len(removed),
            "removed_bytes": removed_bytes,
            "skipped": skipped,
            "permanently_deleted": True,
            "message": f"legacy artifacts purged (removed={len(removed)})",
        }


    def __init__(
        self,
        project_root: Path,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.runtime_root = self.project_root / CLEANER_RUNTIME_RELATIVE_PATH
        self.data_root = self.runtime_root / "business"
        self.backup_root = self.project_root / MANAGED_BACKUP_RELATIVE_ROOT
        self.backup_extract_root = (
            self.project_root / BACKUP_EXTRACT_RELATIVE_ROOT
        )
        self.quarantine_root = self.project_root / QUARANTINE_RELATIVE_PATH
        self.recovery_root = self.project_root / RECOVERY_RELATIVE_PATH
        self.plan_root = self.runtime_root / "plans"
        self.business_history = BusinessHistoryStore(
            self.data_root / "history.sqlite3",
        )
        self.key_path = self.runtime_root / "plan.key"
        self.rules_override_path = self.project_root / "config" / "global-cleaner-rules.json"
        self.progress_callback = progress_callback
        self._hardened_private_paths: set[str] = set()
        self.rules, self.rule_warnings = self._load_rules()
        self._git_tracked_cache: set[str] | None = None
        self._git_status_cache: dict[str, Any] | None = None


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


    def _harden_private_path(self, path: Path) -> None:
        """Best-effort owner-only permissions for plans, keys, and quarantine."""

        resolved = self._safe_resolve(path)
        key = os.path.normcase(str(resolved))
        if key in self._hardened_private_paths or not resolved.exists():
            return
        try:
            resolved.chmod(0o700 if resolved.is_dir() else 0o600)
        except OSError:
            pass
        if os.name == "nt":
            user_name = os.environ.get("USERNAME", "").strip()
            if user_name:
                owner_grant = (
                    f"{user_name}:(OI)(CI)F" if resolved.is_dir() else f"{user_name}:F"
                )
                system_grant = "SYSTEM:(OI)(CI)F" if resolved.is_dir() else "SYSTEM:F"
                try:
                    subprocess.run(
                        [
                            "icacls",
                            str(resolved),
                            "/inheritance:r",
                            "/grant:r",
                            owner_grant,
                            system_grant,
                        ],
                        check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        **_background_subprocess_kwargs(),
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
        self._hardened_private_paths.add(key)


    def _operation_lock_path(self, resource: str) -> Path:
        """Return a contained, non-user-controlled path for an operation lock."""

        runtime_root = self._safe_resolve(self.runtime_root)
        lock_root = self._safe_resolve(runtime_root / "locks")
        try:
            lock_root.relative_to(runtime_root)
        except ValueError as exc:
            raise ValueError("operation lock directory escaped cleaner runtime") from exc
        digest = hashlib.sha256(str(resource).encode("utf-8", errors="replace")).hexdigest()
        lock_path = self._safe_resolve(lock_root / f"{digest}.lock")
        try:
            lock_path.relative_to(lock_root)
        except ValueError as exc:  # Defensive: the digest filename should make this impossible.
            raise ValueError("operation lock escaped the private lock directory") from exc
        return lock_path


    @staticmethod
    def _process_lock_for(path: Path) -> threading.Lock:
        key = os.path.normcase(str(path))
        with _PROCESS_LOCKS_GUARD:
            lock = _PROCESS_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _PROCESS_LOCKS[key] = lock
            return lock


    @staticmethod
    def _try_os_file_lock(handle: BinaryIO) -> tuple[bool, str]:
        """Acquire a one-byte non-blocking lock without PID-based stale cleanup.

        The lock file deliberately remains in place. Kernel locks are released
        automatically when a process exits, so a stale PID is never used as a
        reason to unlink a lock still owned by another process.
        """

        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True, ""
        except (OSError, ImportError) as exc:
            return False, str(exc)


    @staticmethod
    def _release_os_file_lock(handle: BinaryIO) -> None:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass


    @contextmanager
    def _exclusive_resource_lock(
        self,
        resource: str,
        *,
        operation: str,
    ) -> Iterator[dict[str, Any]]:
        try:
            lock_path = self._operation_lock_path(resource)
        except ValueError as exc:
            yield {
                "acquired": False,
                "busy": False,
                "message": f"project cleaner mutation lock is unsafe: {exc}",
            }
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._harden_private_path(lock_path.parent)
        process_lock = self._process_lock_for(lock_path)
        if not process_lock.acquire(blocking=False):
            yield {
                "acquired": False,
                "busy": True,
                "message": "another project cleaner mutation is already running",
            }
            return

        handle: BinaryIO | None = None
        os_locked = False
        acquire_error = ""
        try:
            try:
                lock_path.touch(exist_ok=True)
                handle = lock_path.open("r+b")
                os_locked, acquire_error = self._try_os_file_lock(handle)
            except OSError as exc:
                acquire_error = str(exc)
            if not os_locked or handle is None:
                yield {
                    "acquired": False,
                    "busy": True,
                    "message": (
                        "another project cleaner mutation is already running"
                        if not acquire_error
                        else f"project cleaner mutation lock unavailable: {acquire_error}"
                    ),
                }
                return

            metadata = {
                "schema_version": 1,
                "state": "locked",
                "operation": operation,
                "pid": os.getpid(),
                "acquired_at": self._iso_now(),
            }
            try:
                handle.seek(0)
                handle.write(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                # Lock ownership, not advisory metadata, controls exclusion.
                pass
            yield {"acquired": True, "busy": False, "path": str(lock_path)}
        finally:
            if handle is not None:
                if os_locked:
                    try:
                        released = {
                            "schema_version": 1,
                            "state": "released",
                            "operation": operation,
                            "pid": os.getpid(),
                            "released_at": self._iso_now(),
                        }
                        handle.seek(0)
                        handle.write(
                            json.dumps(released, ensure_ascii=False).encode("utf-8")
                        )
                        handle.truncate()
                        handle.flush()
                        os.fsync(handle.fileno())
                    except OSError:
                        pass
                    self._release_os_file_lock(handle)
                handle.close()
            process_lock.release()


    @contextmanager
    def _mutation_guard(
        self,
        operation: str,
        *,
        batch_name: str = "",
    ) -> Iterator[dict[str, Any]]:
        resources = ["project-mutation"]
        if batch_name:
            resources.append(f"quarantine-batch:{Path(batch_name).name}")
        with ExitStack() as stack:
            for resource in resources:
                lock = stack.enter_context(
                    self._exclusive_resource_lock(resource, operation=operation)
                )
                if not lock.get("acquired"):
                    yield lock
                    return
            yield {"acquired": True, "busy": False}


    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()


    @staticmethod
    def _age_days(path: Path, now: float) -> float:
        try:
            return max(0.0, (now - path.stat(follow_symlinks=False).st_mtime) / SECONDS_PER_DAY)
        except OSError:
            return 0.0


    def _emit_progress(self, phase: str, percent: int, message: str, **payload: Any) -> None:
        if self.progress_callback is None:
            return
        event = {
            "phase": phase,
            "percent": max(0, min(100, int(percent))),
            "message": message,
            "timestamp": self._iso_now(),
            **payload,
        }
        try:
            self.progress_callback(event)
        except Exception:
            pass


    def _append_history(self, action: str, **payload: Any) -> None:
        self.business_history.append(action, **payload)


    def _history_records(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.business_history.read(limit=limit)


    def _unique_quarantine_target(self, batch_dir: Path, rel_path: str) -> Path:
        target = batch_dir / "items" / rel_path
        if not target.exists():
            return target
        for index in range(1, 1000):
            candidate = target.with_name(f"{target.stem}.{index}{target.suffix}")
            if not candidate.exists():
                return candidate
        return target.with_name(f"{target.stem}.{uuid.uuid4().hex[:8]}{target.suffix}")


    @staticmethod
    def _batch_document_sort_key(
        path: Path,
        payload: dict[str, Any],
    ) -> tuple[int, float, int]:
        try:
            revision = max(0, int(payload.get("revision") or 0))
        except (TypeError, ValueError):
            revision = 0
        updated_at = _parse_iso(str(payload.get("updated_at") or ""))
        updated_timestamp = updated_at.timestamp() if updated_at is not None else 0.0
        # The journal is the write-ahead record, so prefer it only when both
        # monotonic revision and timestamp are otherwise identical.
        journal_tiebreaker = int(path.name == "journal.json")
        return revision, updated_timestamp, journal_tiebreaker


    def _valid_batch_documents(
        self,
        batch_dir: Path,
    ) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
        valid: list[tuple[Path, dict[str, Any]]] = []
        errors: list[str] = []
        found = False
        for path in (batch_dir / "manifest.json", batch_dir / "journal.json"):
            if not path.exists():
                continue
            found = True
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                errors.append(f"{path.name}: invalid items")
                continue
            valid.append((path, payload))
        if not found:
            errors.append("quarantine manifest not found")
        return valid, errors


    def _batch_document_path(self, batch_dir: Path) -> Path | None:
        valid, _errors = self._valid_batch_documents(batch_dir)
        if not valid:
            return None
        return max(
            valid,
            key=lambda item: self._batch_document_sort_key(item[0], item[1]),
        )[0]


    def _read_batch_document(self, batch_dir: Path) -> tuple[dict[str, Any] | None, str]:
        valid, errors = self._valid_batch_documents(batch_dir)
        if not valid:
            if errors == ["quarantine manifest not found"]:
                return None, errors[0]
            return None, f"invalid quarantine documents: {'; '.join(errors)}"
        _path, payload = max(
            valid,
            key=lambda item: self._batch_document_sort_key(item[0], item[1]),
        )
        return payload, ""


    def _write_batch_document(self, batch_dir: Path, document: dict[str, Any]) -> None:
        persisted, _error = self._read_batch_document(batch_dir)
        persisted_revision = 0
        if persisted is not None:
            try:
                persisted_revision = max(0, int(persisted.get("revision") or 0))
            except (TypeError, ValueError):
                persisted_revision = 0
        try:
            document_revision = max(0, int(document.get("revision") or 0))
        except (TypeError, ValueError):
            document_revision = 0
        document["revision"] = max(persisted_revision, document_revision) + 1
        document["updated_at"] = self._iso_now()
        self._atomic_write_json(batch_dir / "journal.json", document)
        if str(document.get("status") or "legacy") != "applying":
            self._atomic_write_json(batch_dir / "manifest.json", document)


    @staticmethod
    def _is_restorable_item(item: dict[str, Any]) -> bool:
        status = str(item.get("status") or "")
        return bool(item.get("quarantine_path")) and (
            status in {"moved", "moving", "pending", "error"}
            or (not status and bool(item.get("original_path")))
        )


    def cleanup_garbage(
        self,
        scope: str,
        dry_run: bool = False,
        *,
        quarantine: bool = False,
        quarantine_ttl_hours: int | None = None,
        plan_id: str = "",
        plan_token: str = "",
        selected_item_ids: list[str] | None = None,
        confirm_direct_delete: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            return self._cleanup_garbage_unlocked(
                scope,
                dry_run=True,
                quarantine=quarantine,
                quarantine_ttl_hours=quarantine_ttl_hours,
                plan_id=plan_id,
                plan_token=plan_token,
                selected_item_ids=selected_item_ids,
                confirm_direct_delete=confirm_direct_delete,
            )
        with self._mutation_guard("apply") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "scope": str(scope or ""),
                    "dry_run": False,
                    "quarantine": quarantine,
                    "cleaned_files": 0,
                    "cleaned_dirs": 0,
                    "cleaned_bytes": 0,
                    "permanently_deleted": 0,
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._cleanup_garbage_unlocked(
                scope,
                dry_run=False,
                quarantine=quarantine,
                quarantine_ttl_hours=quarantine_ttl_hours,
                plan_id=plan_id,
                plan_token=plan_token,
                selected_item_ids=selected_item_ids,
                confirm_direct_delete=confirm_direct_delete,
            )


    def _cleanup_garbage_unlocked(
        self,
        scope: str,
        dry_run: bool = False,
        *,
        quarantine: bool = False,
        quarantine_ttl_hours: int | None = None,
        plan_id: str = "",
        plan_token: str = "",
        selected_item_ids: list[str] | None = None,
        confirm_direct_delete: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            plan = self.plan_cleanup(scope)
            return {
                **plan,
                "dry_run": True,
                "quarantine": quarantine,
                "cleaned_files": 0,
                "cleaned_dirs": 0,
                "cleaned_bytes": 0,
                "planned_files": int(plan.get("file_count") or 0),
                "planned_dirs": int(plan.get("dir_count") or 0),
                "planned_bytes": int(plan.get("total_bytes") or 0),
            }

        direct_delete_requested = not quarantine
        plan, plan_error = self._load_plan(plan_id, plan_token)
        if plan is None:
            return {
                "ok": False,
                "scope": str(scope or ""),
                "dry_run": False,
                "quarantine": quarantine,
                "cleaned_files": 0,
                "cleaned_dirs": 0,
                "cleaned_bytes": 0,
                "message": plan_error,
            }
        if direct_delete_requested and not confirm_direct_delete:
            return {
                "ok": False,
                "scope": str(scope or ""),
                "dry_run": False,
                "quarantine": False,
                "error_code": "DIRECT_DELETE_CONFIRMATION_REQUIRED",
                "message": "direct deletion requires explicit confirmation",
            }
        if str(plan.get("scope")) != str(scope or "").strip().lower().replace("project", "global"):
            return {"ok": False, "message": "preview plan scope mismatch", "scope": scope}

        all_items = [item for item in plan.get("items", []) if isinstance(item, dict)]
        selected = {str(item) for item in (selected_item_ids or []) if str(item).strip()}
        items = [item for item in all_items if not selected or str(item.get("item_id")) in selected]
        if selected and len(items) != len(selected):
            return {"ok": False, "message": "selected cleanup items do not match preview plan", "scope": scope}
        if direct_delete_requested and any(
            str(item.get("risk") or "") != "low"
            and item.get("allow_direct_delete") is not True
            for item in items
        ):
            return {
                "ok": False,
                "scope": str(scope or ""),
                "dry_run": False,
                "quarantine": False,
                "error_code": "DIRECT_DELETE_RISK_DENIED",
                "message": "permanent deletion accepts selected low-risk items only",
            }
        ttl_hours = max(1, int(quarantine_ttl_hours or self._quarantine_ttl_hours()))
        created_at = datetime.now(timezone.utc)
        batch_name = f"{created_at.strftime('%Y%m%d_%H%M%S_%f')}-{uuid.uuid4().hex[:8]}"
        batch_dir = self.quarantine_root / batch_name
        document: dict[str, Any] | None = None
        if quarantine:
            batch_dir.mkdir(parents=True, exist_ok=False)
            self._harden_private_path(batch_dir)
            journal_items: list[dict[str, Any]] = []
            reserved_paths: set[str] = set()
            for item in items:
                rel_path = str(item.get("path") or "")
                candidate = (Path("items") / rel_path).as_posix()
                if (
                    not rel_path
                    or Path(rel_path).is_absolute()
                    or ".." in Path(rel_path).parts
                    or candidate.casefold() in reserved_paths
                ):
                    item_id = str(item.get("item_id") or uuid.uuid4().hex)
                    candidate = (Path("items") / item_id / Path(rel_path).name).as_posix()
                reserved_paths.add(candidate.casefold())
                journal_items.append(
                    {
                        **item,
                        "status": "pending",
                        "quarantine_path": candidate,
                        "content_sha256": "",
                    }
                )
            document = {
                "schema_version": QUARANTINE_SCHEMA_VERSION,
                "batch_id": batch_name,
                "status": "applying",
                "created_at": created_at.isoformat(),
                "expires_at": (created_at + timedelta(hours=ttl_hours)).isoformat(),
                "pinned": False,
                "project_root": str(self.project_root),
                "scope": plan.get("scope"),
                "plan_id": plan_id,
                "items": journal_items,
                "errors": [],
                "skipped": [],
            }
            self._write_batch_document(batch_dir, document)

        cleaned_files = 0
        cleaned_dirs = 0
        cleaned_bytes = 0
        apply_errors: list[dict[str, Any]] = []
        apply_skips: list[dict[str, Any]] = []
        moved_entries = document["items"] if document is not None else []
        for index, item in enumerate(items):
            rel_path = str(item.get("path") or "")
            item_type = str(item.get("type") or "")
            target = self.project_root / rel_path
            self._emit_progress(
                "apply",
                5 + int(index / max(1, len(items)) * 90),
                "套用清理計畫",
                current_path=rel_path,
                completed=index,
                total=len(items),
            )
            safe, reason = self._safe_candidate(
                target,
                item_type,
                check_git=not bool(item.get("allow_tracked")),
            )
            if not safe:
                skip = {"path": rel_path, "type": item_type, "reason": reason}
                apply_skips.append(skip)
                if document is not None:
                    moved_entries[index]["status"] = "skipped"
                    moved_entries[index]["status_reason"] = reason
                    document["skipped"] = apply_skips
                    self._write_batch_document(batch_dir, document)
                continue
            try:
                current_snapshot = self._candidate_snapshot(target, item_type)
            except OSError as exc:
                apply_errors.append({"path": rel_path, "type": item_type, "message": str(exc)})
                continue
            expected_fingerprint = item.get("fingerprint") if isinstance(item.get("fingerprint"), dict) else {}
            if current_snapshot.get("digest") != expected_fingerprint.get("digest"):
                reason = "candidate changed after preview"
                apply_skips.append({"path": rel_path, "type": item_type, "reason": reason})
                if document is not None:
                    moved_entries[index]["status"] = "skipped"
                    moved_entries[index]["status_reason"] = reason
                    document["skipped"] = apply_skips
                    self._write_batch_document(batch_dir, document)
                continue
            locked_path = self._locked_descendant(target)
            if locked_path is not None:
                processes = self._locking_processes(locked_path)
                reason = "file is in use; cleaner never forces unlock"
                apply_skips.append(
                    {
                        "path": rel_path,
                        "type": item_type,
                        "reason": reason,
                        "locked_path": self._relative_path(locked_path),
                        "locked_by": processes,
                    }
                )
                if document is not None:
                    moved_entries[index]["status"] = "locked"
                    moved_entries[index]["locked_by"] = processes
                    document["skipped"] = apply_skips
                    self._write_batch_document(batch_dir, document)
                continue
            try:
                size = int(item.get("size_bytes") or 0)
                contents_only = (
                    item_type == "directory"
                    and (
                        bool(self.rules.get("delete_files_only", True))
                        or item.get("contents_only") is True
                    )
                )
                content_file_count = 0
                if quarantine:
                    quarantine_rel = str(moved_entries[index]["quarantine_path"])
                    quarantine_target = (batch_dir / quarantine_rel).resolve()
                    quarantine_target.relative_to(batch_dir.resolve())
                    moved_entries[index]["status"] = "moving"
                    self._write_batch_document(batch_dir, document)
                    if contents_only:
                        quarantine_target.mkdir(parents=True, exist_ok=False)
                        content_files = self._regular_files_below(target)
                        content_file_count = len(content_files)
                        for source_file in content_files:
                            destination_file = (
                                quarantine_target
                                / source_file.relative_to(target)
                            )
                            destination_file.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(source_file), str(destination_file))
                    else:
                        quarantine_target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target), str(quarantine_target))
                    moved_entries[index]["content_sha256"] = self._content_digest(quarantine_target, item_type)
                    moved_entries[index]["status"] = "moved"
                    self._write_batch_document(batch_dir, document)
                else:
                    if item_type == "directory":
                        def clear_readonly_and_retry(
                            operation: Callable[..., Any],
                            path: str,
                            _error: tuple[type[BaseException], BaseException, Any],
                        ) -> None:
                            os.chmod(path, stat_module.S_IWRITE)
                            operation(path)

                        if contents_only:
                            content_files = self._regular_files_below(target)
                            content_file_count = len(content_files)
                            for content_file in content_files:
                                try:
                                    content_file.unlink()
                                except PermissionError:
                                    os.chmod(content_file, stat_module.S_IWRITE)
                                    content_file.unlink()
                        else:
                            shutil.rmtree(target, onerror=clear_readonly_and_retry)
                    else:
                        target.unlink()
                if contents_only:
                    cleaned_files += content_file_count
                else:
                    cleaned_dirs += int(item_type == "directory")
                    cleaned_files += int(item_type == "file")
                cleaned_bytes += size
            except OSError as exc:
                processes = self._locking_processes(target)
                error = {"path": rel_path, "type": item_type, "message": str(exc), "locked_by": processes}
                apply_errors.append(error)
                if document is not None:
                    moved_entries[index]["status"] = "error"
                    moved_entries[index]["status_reason"] = str(exc)
                    document["errors"] = apply_errors
                    self._write_batch_document(batch_dir, document)

        manifest_path = ""
        if document is not None:
            document["status"] = "completed_with_errors" if apply_errors else "completed"
            document["errors"] = apply_errors
            document["skipped"] = apply_skips
            self._write_batch_document(batch_dir, document)
            manifest_path = str(batch_dir / "manifest.json")
            try:
                manifest_digest = self._file_sha256(Path(manifest_path))
                tombstone = {
                    "schema_version": QUARANTINE_SCHEMA_VERSION,
                    "kind": "cleanup-tombstone",
                    "status": "recoverable",
                    "created_at": self._iso_now(),
                    "batch_id": batch_name,
                    "scope": plan.get("scope"),
                    "manifest_path": manifest_path,
                    "manifest_sha256": manifest_digest,
                    "items": [
                        {
                            "original_path": str(item.get("path") or ""),
                            "recovery_path": str(item.get("quarantine_path") or ""),
                            "content_sha256": str(item.get("content_sha256") or ""),
                            "type": str(item.get("type") or ""),
                        }
                        for item in moved_entries
                        if str(item.get("status") or "") == "moved"
                    ],
                }
                self._atomic_write_json(batch_dir / "tombstone.json", tombstone)
            except OSError as exc:
                apply_errors.append(
                    {
                        "path": str(batch_dir),
                        "type": "recovery",
                        "message": f"tombstone publication failed: {exc}",
                    }
                )
        action = "quarantine" if quarantine else "delete"
        result = {
            "ok": not apply_errors,
            "scope": plan.get("scope"),
            "requested_scope": plan.get("requested_scope"),
            "plan_id": plan_id,
            "dry_run": False,
            "quarantine": quarantine,
            "direct_delete_requested": direct_delete_requested,
            "direct_delete_prevented": False,
            "permanently_deleted": (
                cleaned_files + cleaned_dirs if not quarantine else 0
            ),
            "quarantine_path": str(batch_dir) if quarantine else "",
            "quarantine_manifest": manifest_path,
            "cleaned_files": cleaned_files,
            "cleaned_dirs": cleaned_dirs,
            "cleaned_bytes": cleaned_bytes,
            "retained_bytes": cleaned_bytes if quarantine else 0,
            "disk_space_reclaimed_bytes": cleaned_bytes if not quarantine else 0,
            "planned_files": sum(1 for item in items if item.get("type") == "file"),
            "planned_dirs": sum(1 for item in items if item.get("type") == "directory"),
            "planned_bytes": sum(int(item.get("size_bytes") or 0) for item in items),
            "items": items,
            "summary": self._summarize_items(items),
            "health": self._cleanup_health(str(plan.get("scope")), items, apply_skips, apply_errors),
            "skipped": apply_skips,
            "skipped_count": len(apply_skips),
            "errors": apply_errors,
            "error_count": len(apply_errors),
            "message": f"{plan.get('scope')} cleanup {action} completed (items={cleaned_files + cleaned_dirs})",
        }
        self._append_history(
            action,
            ok=result["ok"],
            scope=plan.get("scope"),
            plan_id=plan_id,
            item_count=cleaned_files + cleaned_dirs,
            bytes=cleaned_bytes,
            skipped=len(apply_skips),
            errors=len(apply_errors),
            batch_id=batch_name if quarantine else "",
        )
        try:
            (self.plan_root / f"{plan_id}.json").unlink(missing_ok=True)
        except OSError:
            pass
        self._emit_progress("apply", 100, "清理計畫已完成", cleaned_bytes=cleaned_bytes)
        return result


    def _batch_expiration(self, batch: dict[str, Any], batch_dir: Path) -> datetime:
        expires = _parse_iso(str(batch.get("expires_at") or ""))
        if expires is not None:
            return expires
        try:
            modified = datetime.fromtimestamp(batch_dir.stat(follow_symlinks=False).st_mtime, timezone.utc)
        except OSError:
            modified = datetime.now(timezone.utc)
        return modified + timedelta(hours=self._quarantine_ttl_hours())


    def list_quarantine_batches(self) -> dict[str, Any]:
        batches: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        batch_roots = [
            (self.quarantine_root, False),
            (self.recovery_root / "purged", True),
        ]
        if not any(root.exists() for root, _archived in batch_roots):
            return {
                "ok": True,
                "batches": [],
                "batch_count": 0,
                "total_bytes": 0,
                "health": self._quarantine_health([]),
                "errors": [],
                "message": "quarantine is empty",
            }
        now = datetime.now(timezone.utc)
        for batch_root, archived in batch_roots:
            if not batch_root.exists():
                continue
            for child in sorted(batch_root.iterdir(), key=lambda item: item.name, reverse=True):
                if self._is_link_or_reparse_point(child) or not child.is_dir():
                    continue
                document, error = self._read_batch_document(child)
                if document is None:
                    errors.append({"path": child.name, "message": error})
                    continue
                items = [item for item in document.get("items", []) if isinstance(item, dict)]
                size_bytes = sum(int(item.get("size_bytes") or 0) for item in items if self._is_restorable_item(item))
                expiration = self._batch_expiration(document, child)
                created = _parse_iso(str(document.get("created_at") or "")) or now
                recoverable_items = [item for item in items if self._is_restorable_item(item)]
                batches.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "archived": archived,
                        "created_at": created.isoformat(),
                        "scope": str(document.get("scope") or ""),
                        "status": str(document.get("status") or "legacy"),
                        "item_count": len(items),
                        "restorable_count": len(recoverable_items),
                        "size_bytes": size_bytes,
                        "age_hours": round(max(0.0, (now - created).total_seconds() / 3600), 2),
                        "expires_at": expiration.isoformat(),
                        "expired": False if archived else now >= expiration,
                        "pinned": bool(document.get("pinned")),
                        "recoverable": str(document.get("status")) == "applying"
                        or any(str(item.get("status") or "") in {"pending", "error"} for item in recoverable_items),
                        "manifest_path": str(self._batch_document_path(child) or ""),
                    }
                )
        return {
            "ok": not errors,
            "batches": batches,
            "batch_count": len(batches),
            "total_bytes": sum(int(item.get("size_bytes") or 0) for item in batches),
            "health": self._quarantine_health(batches),
            "errors": errors,
            "message": f"quarantine batches listed (items={len(batches)})",
        }


    def _quarantine_health(self, batches: list[dict[str, Any]]) -> dict[str, Any]:
        total_bytes = sum(int(batch.get("size_bytes") or 0) for batch in batches)
        expired_count = sum(1 for batch in batches if batch.get("expired") and not batch.get("pinned"))
        incomplete_count = sum(1 for batch in batches if batch.get("recoverable"))
        score = max(0, 100 - expired_count * 10 - incomplete_count * 25)
        if not batches:
            state, recommendation = "empty", "隔離區目前沒有批次。"
        elif incomplete_count:
            state, recommendation = "attention", "有中斷交易，建議先還原或完成處理。"
        elif expired_count:
            state, recommendation = "attention", "有過期批次可清理；釘選批次不會自動移除。"
        else:
            state, recommendation = "healthy", "隔離交易完整且可還原。"
        return {
            "state": state,
            "score": score,
            "batch_count": len(batches),
            "expired_count": expired_count,
            "incomplete_count": incomplete_count,
            "pinned_count": sum(1 for batch in batches if batch.get("pinned")),
            "total_bytes": total_bytes,
            "ttl_hours": self._quarantine_ttl_hours(),
            "recommendation": recommendation,
        }


    def set_quarantine_pinned(self, batch_name: str, pinned: bool) -> dict[str, Any]:
        clean_name = Path(str(batch_name or "").strip()).name
        if not clean_name:
            return {"ok": False, "message": "quarantine batch is required"}
        with self._mutation_guard("pin" if pinned else "unpin", batch_name=clean_name) as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._set_quarantine_pinned_unlocked(clean_name, pinned)


    def _set_quarantine_pinned_unlocked(
        self, clean_name: str, pinned: bool
    ) -> dict[str, Any]:
        batch_dir = self.quarantine_root / clean_name
        if not batch_dir.exists():
            archived = self.recovery_root / "purged" / clean_name
            if archived.exists():
                batch_dir = archived
        document, error = self._read_batch_document(batch_dir)
        if document is None:
            return {"ok": False, "message": error}
        document["pinned"] = bool(pinned)
        self._write_batch_document(batch_dir, document)
        self._append_history("pin" if pinned else "unpin", ok=True, batch_id=clean_name)
        return {"ok": True, "batch": clean_name, "pinned": bool(pinned), "message": "quarantine pin updated"}


    def purge_quarantine(
        self,
        older_than_hours: int | None = None,
        *,
        permanent: bool = False,
    ) -> dict[str, Any]:
        with self._mutation_guard("purge") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "purged_dirs": 0,
                    "purged_bytes": 0,
                    "permanently_deleted": 0,
                    "errors": [],
                    "skipped": [],
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._purge_quarantine_unlocked(
                older_than_hours,
                permanent=permanent,
            )


    def _purge_quarantine_unlocked(
        self,
        older_than_hours: int | None = None,
        *,
        permanent: bool = False,
    ) -> dict[str, Any]:
        ttl = max(1, int(older_than_hours or self._quarantine_ttl_hours()))
        purged_dirs = 0
        purged_bytes = 0
        errors: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        archives: list[dict[str, Any]] = []
        if not self.quarantine_root.exists():
            return {
                "ok": True,
                "purged_dirs": 0,
                "purged_bytes": 0,
                "archived_dirs": 0,
                "archived_bytes": 0,
                "archives": [],
                "permanently_deleted": 0,
                "errors": [],
                "skipped": [],
                "message": "quarantine is empty",
            }
        now = datetime.now(timezone.utc)
        for child in sorted(self.quarantine_root.iterdir(), key=lambda item: item.name):
            if self._is_link_or_reparse_point(child) or not child.is_dir():
                continue
            document, error = self._read_batch_document(child)
            if document is None:
                skipped.append({"path": child.name, "reason": error})
                continue
            recoverable = any(
                isinstance(item, dict)
                and item.get("status") in {"pending", "error"}
                and item.get("quarantine_path")
                for item in document.get("items", [])
            )
            if document.get("pinned") or str(document.get("status")) == "applying" or recoverable:
                skipped.append({"path": child.name, "reason": "pinned or recoverable batch"})
                continue
            created = _parse_iso(str(document.get("created_at") or "")) or now
            configured_expiration = self._batch_expiration(document, child)
            requested_expiration = created + timedelta(hours=ttl)
            if not permanent and now < min(
                configured_expiration,
                requested_expiration,
            ):
                continue
            try:
                size = sum(int(item.get("size_bytes") or 0) for item in document.get("items", []) if isinstance(item, dict))
                integrity_errors: list[str] = []
                for item in document.get("items", []):
                    if not isinstance(item, dict) or not self._is_restorable_item(item):
                        continue
                    relative = str(item.get("quarantine_path") or "").strip()
                    source = (child / relative).resolve()
                    try:
                        source.relative_to(child.resolve())
                    except ValueError:
                        integrity_errors.append(f"{relative}: escaped batch")
                        continue
                    if not source.exists() or self._is_link_or_reparse_point(source):
                        integrity_errors.append(f"{relative}: missing or unsafe")
                        continue
                    expected = str(item.get("content_sha256") or "")
                    actual = self._content_digest(
                        source,
                        str(item.get("type") or "file"),
                    )
                    if not expected or actual != expected:
                        integrity_errors.append(f"{relative}: SHA-256 mismatch")
                if integrity_errors:
                    skipped.append(
                        {
                            "path": child.name,
                            "reason": "; ".join(integrity_errors),
                        }
                    )
                    continue

                if permanent:
                    locked_path = self._locked_descendant(child)
                    if locked_path is not None:
                        skipped.append(
                            {
                                "path": child.name,
                                "reason": "batch contains an in-use file",
                            }
                        )
                        continue
                    shutil.rmtree(child)
                    purged_dirs += 1
                    purged_bytes += size
                    continue

                archive_root = self.recovery_root / "purged"
                archive_root.mkdir(parents=True, exist_ok=True)
                self._harden_private_path(self.recovery_root)
                self._harden_private_path(archive_root)
                archive_dir = archive_root / child.name
                if archive_dir.exists():
                    skipped.append(
                        {
                            "path": child.name,
                            "reason": "recovery archive destination already exists",
                        }
                    )
                    continue
                os.replace(child, archive_dir)
                document["status"] = "archived"
                document["archived_at"] = self._iso_now()
                document["archive_path"] = str(archive_dir)
                self._write_batch_document(archive_dir, document)
                manifest_document = (
                    archive_dir / "manifest.json"
                    if (archive_dir / "manifest.json").exists()
                    else archive_dir / "journal.json"
                )
                manifest_sha256 = self._file_sha256(manifest_document)
                purge_tombstone = {
                    "schema_version": QUARANTINE_SCHEMA_VERSION,
                    "kind": "purge-tombstone",
                    "status": "recoverable",
                    "created_at": self._iso_now(),
                    "batch_id": child.name,
                    "original_path": str(child),
                    "recovery_path": str(archive_dir),
                    "manifest_path": str(manifest_document),
                    "manifest_sha256": manifest_sha256,
                    "permanently_deleted": 0,
                }
                self._atomic_write_json(
                    archive_dir / "purge-tombstone.json",
                    purge_tombstone,
                )
                purged_dirs += 1
                purged_bytes += size
                archives.append(purge_tombstone)
            except OSError as exc:
                errors.append({"path": str(child), "message": str(exc)})
        result = {
            "ok": not errors,
            "purged_dirs": purged_dirs,
            "purged_bytes": purged_bytes,
            "archived_dirs": purged_dirs,
            "archived_bytes": purged_bytes,
            "disk_space_reclaimed_bytes": purged_bytes if permanent else 0,
            "archives": archives,
            "permanently_deleted": purged_dirs if permanent else 0,
            "errors": errors,
            "skipped": skipped,
            "message": (
                f"quarantine permanently deleted (dirs={purged_dirs})"
                if permanent
                else f"quarantine archival completed (dirs={purged_dirs})"
            ),
        }
        self._append_history("purge", ok=result["ok"], item_count=purged_dirs, bytes=purged_bytes, errors=len(errors))
        return result


    @staticmethod
    def _unique_restore_destination(destination: Path) -> Path:
        for index in range(1, 1000):
            candidate = destination.with_name(f"{destination.stem}.restored-{index}{destination.suffix}")
            if not candidate.exists():
                return candidate
        return destination.with_name(f"{destination.stem}.restored-{uuid.uuid4().hex[:8]}{destination.suffix}")


    def restore_quarantine(
        self, batch_name: str, *, conflict_strategy: str = "skip"
    ) -> dict[str, Any]:
        clean_name = Path(str(batch_name or "").strip()).name
        if not clean_name:
            return {"ok": False, "message": "quarantine batch is required"}
        with self._mutation_guard("restore", batch_name=clean_name) as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "restored": 0,
                    "renamed": 0,
                    "skipped": [],
                    "errors": [],
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._restore_quarantine_unlocked(
                clean_name,
                conflict_strategy=conflict_strategy,
            )


    def _restore_quarantine_unlocked(
        self, batch_name: str, *, conflict_strategy: str = "skip"
    ) -> dict[str, Any]:
        clean_name = Path(str(batch_name or "").strip()).name
        live_batch = self.quarantine_root / clean_name
        archived_batch = self.recovery_root / "purged" / clean_name
        selected_root = self.quarantine_root
        selected_batch = live_batch
        if not live_batch.exists() and archived_batch.exists():
            selected_root = self.recovery_root / "purged"
            selected_batch = archived_batch
        batch_dir = selected_batch.resolve()
        try:
            batch_dir.relative_to(selected_root.resolve())
        except ValueError:
            return {"ok": False, "message": "invalid quarantine batch"}
        document, error = self._read_batch_document(batch_dir)
        if document is None:
            return {"ok": False, "message": error}
        strategy = conflict_strategy if conflict_strategy in {"skip", "rename"} else "skip"
        restored = 0
        renamed = 0
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        items = [item for item in document.get("items", []) if isinstance(item, dict)]
        for index, item in enumerate(items):
            if not self._is_restorable_item(item):
                continue
            original_rel = str(item.get("path") or item.get("original_path") or "").strip()
            quarantine_rel = str(item.get("quarantine_path") or "").strip()
            if not original_rel or not quarantine_rel:
                continue
            source = (batch_dir / quarantine_rel).resolve()
            destination = (self.project_root / original_rel).resolve()
            try:
                source.relative_to(batch_dir)
                destination.relative_to(self.project_root)
            except ValueError:
                skipped.append({"path": original_rel, "reason": "invalid restore path"})
                continue
            if not source.exists() or self._is_link_or_reparse_point(source):
                skipped.append({"path": original_rel, "reason": "quarantine item missing or unsafe"})
                continue
            expected_digest = str(item.get("content_sha256") or "")
            if expected_digest:
                try:
                    actual_digest = self._content_digest(source, str(item.get("type") or "file"))
                except OSError as exc:
                    errors.append({"path": original_rel, "message": str(exc)})
                    continue
                if actual_digest != expected_digest:
                    skipped.append({"path": original_rel, "reason": "quarantine integrity mismatch"})
                    continue
            if item.get("contents_only") is True:
                if destination.exists() and not destination.is_dir():
                    skipped.append(
                        {
                            "path": original_rel,
                            "reason": "destination is not a directory",
                        }
                    )
                    continue
                try:
                    destination.mkdir(parents=True, exist_ok=True)
                    source_files = self._regular_files_below(source)
                    item_skipped = False
                    for source_file in source_files:
                        relative_file = source_file.relative_to(source)
                        destination_file = destination / relative_file
                        if destination_file.exists():
                            if strategy == "rename":
                                destination_file = self._unique_restore_destination(
                                    destination_file
                                )
                                renamed += 1
                            else:
                                skipped.append(
                                    {
                                        "path": (
                                            Path(original_rel) / relative_file
                                        ).as_posix(),
                                        "reason": "destination already exists",
                                    }
                                )
                                item_skipped = True
                                continue
                        destination_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(source_file), str(destination_file))
                        restored += 1
                    if not item_skipped:
                        item["status"] = "restored"
                        item["restored_path"] = original_rel
                    document["items"] = items
                    self._write_batch_document(batch_dir, document)
                except OSError as exc:
                    errors.append(
                        {
                            "path": original_rel,
                            "message": str(exc),
                            "locked_by": self._locking_processes(destination),
                        }
                    )
                continue
            if destination.exists():
                if strategy == "rename":
                    destination = self._unique_restore_destination(destination)
                    renamed += 1
                else:
                    skipped.append({"path": original_rel, "reason": "destination already exists"})
                    continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                item["status"] = "restored"
                item["restored_path"] = destination.relative_to(self.project_root).as_posix()
                restored += 1
                document["items"] = items
                self._write_batch_document(batch_dir, document)
            except OSError as exc:
                errors.append({"path": original_rel, "message": str(exc), "locked_by": self._locking_processes(destination)})
        if not any(item.get("status") == "moved" for item in items):
            document["status"] = "restored"
        document["items"] = items
        document["restore_errors"] = errors
        document["restore_skips"] = skipped
        self._write_batch_document(batch_dir, document)
        result = {
            "ok": not errors,
            "restored": restored,
            "renamed": renamed,
            "conflict_strategy": strategy,
            "skipped": skipped,
            "errors": errors,
            "message": f"quarantine restore completed (items={restored})",
        }
        self._append_history("restore", ok=result["ok"], batch_id=clean_name, item_count=restored, renamed=renamed, errors=len(errors))
        return result


    @staticmethod
    def _process_is_alive(process_id: int) -> bool:
        if process_id <= 0:
            return False
        if process_id == os.getpid():
            return True
        if os.name == "nt":
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, process_id)
            if not process:
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(
                    process,
                    ctypes.byref(exit_code),
                ):
                    return True
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(process)
        try:
            os.kill(process_id, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False


    @staticmethod
    def _is_sha256_text(value: str) -> bool:
        normalized = str(value or "").strip()
        return len(normalized) == 64 and all(
            character in "0123456789abcdefABCDEF"
            for character in normalized
        )


    def _repair_anomaly_candidates(
        self,
        *,
        include_shared: bool,
        now: float | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Find only deterministic anomalies that have a recoverable repair."""

        current_time = time.time() if now is None else float(now)
        candidates: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_candidate(
            path: Path,
            *,
            layer: str,
            anomaly: str,
            reason: str,
        ) -> None:
            absolute = Path(os.path.abspath(path))
            key = os.path.normcase(str(absolute))
            if key in seen or not absolute.exists():
                return
            seen.add(key)
            if not self._inside_project(absolute):
                diagnostics.append(
                    {
                        "path": str(absolute),
                        "layer": layer,
                        "anomaly": anomaly,
                        "repairable": False,
                        "reason": "path is outside project root",
                    }
                )
                return
            if self._is_link_or_reparse_point(absolute):
                diagnostics.append(
                    {
                        "path": self._relative_path(absolute),
                        "layer": layer,
                        "anomaly": anomaly,
                        "repairable": False,
                        "reason": "link or reparse point requires manual review",
                    }
                )
                return
            item_type = "directory" if absolute.is_dir() else "file"
            if item_type == "file" and not absolute.is_file():
                return
            try:
                snapshot = self._candidate_snapshot(absolute, item_type)
            except OSError as exc:
                diagnostics.append(
                    {
                        "path": self._relative_path(absolute),
                        "layer": layer,
                        "anomaly": anomaly,
                        "repairable": False,
                        "reason": str(exc),
                    }
                )
                return
            candidates.append(
                {
                    "item_id": uuid.uuid4().hex,
                    "path": self._relative_path(absolute),
                    "type": item_type,
                    "size_bytes": int(snapshot.get("size_bytes") or 0),
                    "fingerprint": snapshot,
                    "layer": layer,
                    "anomaly": anomaly,
                    "reason": reason,
                    "risk": "low",
                    "repairable": True,
                }
            )

        if self.plan_root.is_dir() and not self._is_link_or_reparse_point(
            self.plan_root
        ):
            for path in sorted(self.plan_root.iterdir(), key=lambda item: item.name):
                if self._is_link_or_reparse_point(path) or not path.is_file():
                    continue
                if path.suffix.casefold() == ".tmp":
                    try:
                        age_seconds = current_time - path.stat(
                            follow_symlinks=False
                        ).st_mtime
                    except OSError:
                        continue
                    if age_seconds >= 10 * 60:
                        add_candidate(
                            path,
                            layer="cleaner",
                            anomaly="orphan-atomic-temp",
                            reason="stale atomic-write temporary file",
                        )
                    continue
                if path.suffix.casefold() != ".json":
                    continue
                invalid_reason = ""
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                    expires_at = (
                        _parse_iso(str(document.get("expires_at") or ""))
                        if isinstance(document, dict)
                        else None
                    )
                    valid_identity = (
                        isinstance(document, dict)
                        and int(document.get("schema_version") or 0)
                        == PLAN_SCHEMA_VERSION
                        and str(document.get("plan_id") or "") == path.stem
                        and self._is_sha256_text(
                            str(document.get("plan_token") or "")
                        )
                    )
                    if not valid_identity:
                        invalid_reason = "invalid preview-plan document"
                    elif (
                        expires_at is None
                        or expires_at <= datetime.now(timezone.utc)
                    ):
                        invalid_reason = "expired preview plan"
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    invalid_reason = "unreadable preview-plan document"
                if invalid_reason:
                    add_candidate(
                        path,
                        layer="cleaner",
                        anomaly="stale-preview-plan",
                        reason=invalid_reason,
                    )

        if include_shared:
            build_stamp = (
                self.project_root
                / "launcher"
                / "state"
                / "production-build.sha256"
            )
            if build_stamp.is_file() and not self._is_link_or_reparse_point(
                build_stamp
            ):
                try:
                    stamp_value = build_stamp.read_text(encoding="ascii")
                except (OSError, UnicodeError):
                    stamp_value = ""
                if not self._is_sha256_text(stamp_value):
                    add_candidate(
                        build_stamp,
                        layer="shared",
                        anomaly="invalid-production-signature",
                        reason=(
                            "invalid production signature; launcher will rebuild "
                            "instead of mixing generations"
                        ),
                    )

            tools_root = self.project_root / "platform_tools"
            if tools_root.is_dir() and not self._is_link_or_reparse_point(
                tools_root
            ):
                for tool_dir in sorted(
                    tools_root.iterdir(),
                    key=lambda item: item.name,
                ):
                    build_root = tool_dir / "build"
                    if (
                        not tool_dir.is_dir()
                        or self._is_link_or_reparse_point(tool_dir)
                        or not build_root.is_dir()
                        or self._is_link_or_reparse_point(build_root)
                    ):
                        continue
                    for lock_path in sorted(
                        build_root.glob(".package-*.lock"),
                        key=lambda item: item.name,
                    ):
                        if (
                            not lock_path.is_dir()
                            or self._is_link_or_reparse_point(lock_path)
                        ):
                            continue
                        try:
                            age_seconds = current_time - lock_path.stat(
                                follow_symlinks=False
                            ).st_mtime
                        except OSError:
                            continue
                        owner: dict[str, Any] = {}
                        owner_path = lock_path / "owner.json"
                        if (
                            owner_path.is_file()
                            and not self._is_link_or_reparse_point(owner_path)
                        ):
                            try:
                                loaded_owner = json.loads(
                                    owner_path.read_text(encoding="utf-8")
                                )
                                if isinstance(loaded_owner, dict):
                                    owner = loaded_owner
                            except (OSError, json.JSONDecodeError):
                                owner = {}
                        owner_pid = owner.get("pid")
                        if isinstance(owner_pid, int) and self._process_is_alive(
                            owner_pid
                        ):
                            continue
                        if age_seconds >= 30:
                            add_candidate(
                                lock_path,
                                layer="package",
                                anomaly="stale-package-lock",
                                reason="package owner process is no longer running",
                            )

                    for recovered_lock in sorted(
                        build_root.glob("package-lock-recovery-*"),
                        key=lambda item: item.name,
                    ):
                        try:
                            age_seconds = current_time - recovered_lock.stat(
                                follow_symlinks=False
                            ).st_mtime
                        except OSError:
                            continue
                        if age_seconds >= 60 * 60:
                            add_candidate(
                                recovered_lock,
                                layer="package",
                                anomaly="retired-package-lock",
                                reason="stale package lock was already retired",
                            )

                    completed: list[Path] = []
                    for package_root in build_root.glob("package-*"):
                        if (
                            not package_root.is_dir()
                            or self._is_link_or_reparse_point(package_root)
                            or not (
                                package_root / "promotion-complete.json"
                            ).is_file()
                            or not (
                                package_root
                                / "promotion-recovery-manifest.json"
                            ).is_file()
                        ):
                            continue
                        aborted_path = package_root / "promotion-aborted.json"
                        if aborted_path.is_file():
                            try:
                                aborted = json.loads(
                                    aborted_path.read_text(encoding="utf-8")
                                )
                            except (OSError, json.JSONDecodeError):
                                aborted = {}
                            if (
                                isinstance(aborted, dict)
                                and aborted.get("status")
                                == "rollback-incomplete"
                            ):
                                continue
                        completed.append(package_root)
                    completed.sort(
                        key=lambda path: path.stat(
                            follow_symlinks=False
                        ).st_mtime_ns,
                        reverse=True,
                    )
                    for excess in completed[1:]:
                        add_candidate(
                            excess,
                            layer="package",
                            anomaly="excess-completed-recovery",
                            reason=(
                                "older successful package recovery exceeds "
                                "the one-generation retention boundary"
                            ),
                        )

        for warning in self.rule_warnings:
            diagnostics.append(
                {
                    "path": self._relative_path(self.rules_override_path)
                    if self._inside_project(self.rules_override_path)
                    else str(self.rules_override_path),
                    "layer": "shared",
                    "anomaly": "rule-warning",
                    "repairable": False,
                    "reason": warning,
                }
            )
        return candidates, diagnostics


    def repair_anomalies(
        self,
        *,
        dry_run: bool = False,
        include_shared: bool = True,
        selected_anomalies: set[str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        candidates, diagnostics = self._repair_anomaly_candidates(
            include_shared=include_shared,
            now=now,
        )
        if selected_anomalies is not None:
            normalized_selection = {
                str(item).strip()
                for item in selected_anomalies
                if str(item).strip()
            }
            candidates = [
                item
                for item in candidates
                if str(item.get("anomaly") or "") in normalized_selection
            ]
        base_result: dict[str, Any] = {
            "ok": True,
            "dry_run": dry_run,
            "include_shared": include_shared,
            "selected_anomalies": (
                sorted(normalized_selection)
                if selected_anomalies is not None
                else None
            ),
            "anomaly_count": len(candidates) + len(diagnostics),
            "repairable_count": len(candidates),
            "repaired_count": 0,
            "repaired_bytes": 0,
            "permanently_deleted": 0,
            "disk_space_reclaimed_bytes": 0,
            "items": candidates,
            "diagnostics": diagnostics,
            "errors": [],
            "message": (
                f"anomaly scan completed (repairable={len(candidates)}, "
                f"diagnostic={len(diagnostics)})"
            ),
        }
        if dry_run or not candidates:
            return base_result

        with self._mutation_guard("anomaly-repair") as lock:
            if not lock.get("acquired"):
                return {
                    **base_result,
                    "ok": False,
                    "busy": True,
                    "message": str(
                        lock.get("message") or "project cleaner is busy"
                    ),
                }

            created_at = datetime.now(timezone.utc)
            batch_name = (
                f"repair-{created_at.strftime('%Y%m%d_%H%M%S_%f')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            batch_dir = self.quarantine_root / batch_name
            batch_dir.mkdir(parents=True, exist_ok=False)
            self._harden_private_path(batch_dir)
            document: dict[str, Any] = {
                "schema_version": QUARANTINE_SCHEMA_VERSION,
                "batch_id": batch_name,
                "status": "applying",
                "created_at": created_at.isoformat(),
                "expires_at": (
                    created_at + timedelta(hours=self._quarantine_ttl_hours())
                ).isoformat(),
                "pinned": False,
                "project_root": str(self.project_root),
                "scope": "anomaly-repair",
                "plan_id": "",
                "items": [
                    {
                        **item,
                        "original_path": item["path"],
                        "quarantine_path": (
                            Path("items") / str(item["path"])
                        ).as_posix(),
                        "content_sha256": "",
                        "status": "pending",
                    }
                    for item in candidates
                ],
                "errors": [],
                "skipped": [],
            }
            self._write_batch_document(batch_dir, document)

            repaired_count = 0
            repaired_bytes = 0
            errors: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            entries = document["items"]
            for index, item in enumerate(candidates):
                relative_path = str(item["path"])
                target = self.project_root / relative_path
                recovery_target = (
                    batch_dir / str(entries[index]["quarantine_path"])
                )
                self._emit_progress(
                    "repair",
                    5 + int(index / max(1, len(candidates)) * 90),
                    "Repairing recoverable project anomaly",
                    current_path=relative_path,
                    completed=index,
                    total=len(candidates),
                )
                if (
                    not target.exists()
                    or self._is_link_or_reparse_point(target)
                ):
                    skip = {
                        "path": relative_path,
                        "reason": "anomaly changed before repair",
                    }
                    skipped.append(skip)
                    entries[index]["status"] = "skipped"
                    entries[index]["status_reason"] = skip["reason"]
                    document["skipped"] = skipped
                    self._write_batch_document(batch_dir, document)
                    continue
                try:
                    current_snapshot = self._candidate_snapshot(
                        target,
                        str(item["type"]),
                    )
                except OSError as exc:
                    errors.append(
                        {"path": relative_path, "message": str(exc)}
                    )
                    entries[index]["status"] = "error"
                    entries[index]["status_reason"] = str(exc)
                    document["errors"] = errors
                    self._write_batch_document(batch_dir, document)
                    continue
                expected = item.get("fingerprint", {})
                if current_snapshot.get("digest") != expected.get("digest"):
                    skip = {
                        "path": relative_path,
                        "reason": "anomaly changed after scan",
                    }
                    skipped.append(skip)
                    entries[index]["status"] = "skipped"
                    entries[index]["status_reason"] = skip["reason"]
                    document["skipped"] = skipped
                    self._write_batch_document(batch_dir, document)
                    continue
                if self._is_locked(target):
                    skip = {
                        "path": relative_path,
                        "reason": "path is in use; cleaner never forces unlock",
                        "locked_by": self._locking_processes(target),
                    }
                    skipped.append(skip)
                    entries[index]["status"] = "locked"
                    entries[index]["locked_by"] = skip["locked_by"]
                    document["skipped"] = skipped
                    self._write_batch_document(batch_dir, document)
                    continue
                try:
                    recovery_target.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    os.replace(target, recovery_target)
                    entries[index]["content_sha256"] = self._content_digest(
                        recovery_target,
                        str(item["type"]),
                    )
                    entries[index]["status"] = "moved"
                    repaired_count += 1
                    repaired_bytes += int(item.get("size_bytes") or 0)
                except OSError as exc:
                    errors.append(
                        {"path": relative_path, "message": str(exc)}
                    )
                    entries[index]["status"] = "error"
                    entries[index]["status_reason"] = str(exc)
                document["errors"] = errors
                document["skipped"] = skipped
                self._write_batch_document(batch_dir, document)

            document["status"] = (
                "completed_with_errors" if errors else "completed"
            )
            self._write_batch_document(batch_dir, document)
            manifest_path = batch_dir / "manifest.json"
            if manifest_path.is_file():
                self._atomic_write_json(
                    batch_dir / "tombstone.json",
                    {
                        "schema_version": QUARANTINE_SCHEMA_VERSION,
                        "kind": "repair-tombstone",
                        "status": "recoverable",
                        "created_at": self._iso_now(),
                        "batch_id": batch_name,
                        "scope": "anomaly-repair",
                        "manifest_path": str(manifest_path),
                        "manifest_sha256": self._file_sha256(manifest_path),
                        "items": [
                            {
                                "original_path": str(
                                    item.get("original_path") or ""
                                ),
                                "recovery_path": str(
                                    item.get("quarantine_path") or ""
                                ),
                                "content_sha256": str(
                                    item.get("content_sha256") or ""
                                ),
                                "type": str(item.get("type") or ""),
                            }
                            for item in entries
                            if str(item.get("status") or "") == "moved"
                        ],
                    },
                )

            result = {
                **base_result,
                "ok": not errors,
                "repaired_count": repaired_count,
                "repaired_bytes": repaired_bytes,
                "quarantine": True,
                "quarantine_path": str(batch_dir),
                "quarantine_manifest": str(manifest_path),
                "items": entries,
                "skipped": skipped,
                "errors": errors,
                "message": (
                    "anomaly repair completed "
                    f"(repaired={repaired_count}, skipped={len(skipped)}, "
                    f"errors={len(errors)})"
                ),
            }
            self._append_history(
                "anomaly-repair",
                ok=result["ok"],
                scope="global" if include_shared else "runtime",
                item_count=repaired_count,
                bytes=repaired_bytes,
                skipped=len(skipped),
                errors=len(errors),
                batch_id=batch_name,
            )
            self._emit_progress(
                "repair",
                100,
                "Recoverable anomaly repair completed",
                repaired_count=repaired_count,
            )
            return result


    def _system_rescue_subprocess(
        self,
        command: list[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        environment = os.environ.copy()
        environment.pop("ELECTRON_RUN_AS_NODE", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.project_root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                **_background_subprocess_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "exit_code": 124
                if isinstance(exc, subprocess.TimeoutExpired)
                else -1,
                "output": str(exc),
            }
        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "output": completed.stdout[-12000:],
        }


    def _system_rescue_package_check(self) -> dict[str, Any]:
        script = (
            self.project_root
            / "system-rescue"
            / "src"
            / "backend"
            / "services"
            / "system_rescue"
            / "integration"
            / "platform_packager.py"
        )
        if (
            not script.is_file()
            or self._is_link_or_reparse_point(script)
            or not self._inside_project(script)
        ):
            return {
                "ok": False,
                "available": False,
                "message": "package verification entry is unavailable",
                "results": [],
            }
        run = self._system_rescue_subprocess(
            [
                sys.executable,
                "-B",
                "-s",
                str(script),
                "--all",
                "--verify",
                "--json",
            ],
            timeout_seconds=180,
        )
        payload: dict[str, Any] = {}
        for line in reversed(str(run.get("output") or "").splitlines()):
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict):
                payload = loaded
                break
        results = payload.get("results", [])
        if not isinstance(results, list):
            results = []
        package_ok = bool(run.get("ok")) and payload.get("ok") is True
        deferred_results = [
            item
            for item in results
            if isinstance(item, dict)
            and (
                item.get("error_code") == "STALE_PACKAGE"
                or (
                    item.get("tool_id") == "governance_rule"
                    and item.get("error_code") == "PACKAGE_MISSING"
                )
            )
        ]
        blocking_results = [
            item for item in results if item not in deferred_results
        ]
        packaging_deferred = bool(deferred_results) and not blocking_results
        return {
            "ok": package_ok or packaging_deferred,
            "available": True,
            "exit_code": run.get("exit_code"),
            "results": results,
            "blocking_results": blocking_results,
            "deferred_results": deferred_results,
            "packaging_deferred": packaging_deferred,
            "message": (
                "all packaged tools are current"
                if package_ok
                else (
                    "existing packages are structurally usable; source freshness "
                    "is deferred until packaging is explicitly requested"
                    if packaging_deferred
                    else "one or more packaged tools failed integrity verification"
                )
            ),
            "output": str(run.get("output") or "")[-4000:],
        }


    @staticmethod
    def _system_rescue_main_health() -> dict[str, Any]:
        try:
            with urllib_request.urlopen(
                "http://127.0.0.1:8765/health",
                timeout=2,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            OSError,
            TimeoutError,
            UnicodeError,
            json.JSONDecodeError,
            urllib_error.URLError,
        ) as exc:
            return {
                "ok": False,
                "reachable": False,
                "message": f"main backend is unavailable: {exc}",
            }
        return {
            "ok": payload.get("ok") is True,
            "reachable": True,
            "runtime_state": payload.get("runtime_state"),
            "phase": payload.get("phase"),
            "workspace_instance_id": payload.get("workspace_instance_id"),
            "message": "main backend health endpoint responded",
        }


    def system_health_check(self, *, deep: bool = False) -> dict[str, Any]:
        """Run global read-only system diagnostics owned by Global Cleaner."""

        self._emit_progress("rescue", 5, "Checking project boundary")
        required: list[dict[str, Any]] = []
        for relative_path in SYSTEM_RESCUE_REQUIRED_PATHS:
            target = self.project_root / relative_path
            valid = False
            reason = ""
            try:
                valid = (
                    self._inside_project(target)
                    and target.is_file()
                    and not self._is_link_or_reparse_point(target)
                )
                if not valid:
                    reason = "missing, non-regular, or outside project boundary"
            except OSError as exc:
                reason = str(exc)
            required.append(
                {
                    "path": relative_path,
                    "ok": valid,
                    "reason": reason,
                }
            )

        self._emit_progress("rescue", 25, "Validating system configuration")
        configurations: list[dict[str, Any]] = []
        for relative_path in (
            "main-system/package.json",
            "main-system/config/tool-runtime-contract.json",
        ):
            target = self.project_root / relative_path
            valid = False
            reason = ""
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                valid = isinstance(loaded, dict)
                if not valid:
                    reason = "configuration root must be an object"
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                reason = str(exc)
            configurations.append(
                {
                    "path": relative_path,
                    "ok": valid,
                    "reason": reason,
                }
            )

        self._emit_progress("rescue", 45, "Checking package integrity")
        packages = self._system_rescue_package_check()
        anomalies = self.repair_anomalies(
            dry_run=True,
            include_shared=True,
            selected_anomalies=set(SYSTEM_RESCUE_REPAIR_ANOMALIES),
        )
        environment = {
            "python": {
                "ok": Path(sys.executable).is_file(),
                "path": sys.executable,
            },
            "node": {
                "ok": shutil.which("node") is not None,
                "path": shutil.which("node") or "",
            },
            "npm": {
                "ok": shutil.which("npm.cmd" if os.name == "nt" else "npm")
                is not None,
                "path": shutil.which(
                    "npm.cmd" if os.name == "nt" else "npm"
                )
                or "",
            },
        }
        main_health = self._system_rescue_main_health()
        typecheck: dict[str, Any] = {
            "ok": True,
            "skipped": True,
            "message": "deep type check was not requested",
        }
        if deep:
            self._emit_progress("rescue", 70, "Running deep type check")
            npm = "npm.cmd" if os.name == "nt" else "npm"
            typecheck = self._system_rescue_subprocess(
                [
                    npm,
                    "--prefix",
                    str(self.project_root / "main-system"),
                    "run",
                    "type-check",
                ],
                timeout_seconds=180,
            )
            typecheck["skipped"] = False
            typecheck["message"] = (
                "type check passed"
                if typecheck.get("ok")
                else "type check failed"
            )

        blocking: list[str] = []
        blocking.extend(
            f"required:{item['path']}"
            for item in required
            if not item["ok"]
        )
        blocking.extend(
            f"config:{item['path']}"
            for item in configurations
            if not item["ok"]
        )
        if not packages.get("ok"):
            blocking.append("package-integrity")
        if not typecheck.get("ok"):
            blocking.append("type-check")
        repairable_count = int(anomalies.get("repairable_count") or 0)
        state = (
            "blocked"
            if blocking
            else "repairable"
            if repairable_count
            else "healthy"
        )
        result = {
            "ok": not blocking,
            "operation": "system-health-check",
            "state": state,
            "deep": deep,
            "project_root": str(self.project_root),
            "authority": "global-cleaner",
            "boundary": "project-only",
            "required_paths": required,
            "configurations": configurations,
            "environment": environment,
            "packages": packages,
            "main_health": main_health,
            "typecheck": typecheck,
            "anomalies": anomalies,
            "repairable_count": repairable_count,
            "blocking": blocking,
            "message": (
                "system rescue check passed"
                if state == "healthy"
                else "system rescue found recoverable anomalies"
                if state == "repairable"
                else "system rescue found blocking problems"
            ),
        }
        self._append_history(
            "system-health-check",
            ok=result["ok"],
            scope="global",
            item_count=repairable_count,
            errors=len(blocking),
        )
        self._emit_progress(
            "rescue",
            100,
            "System rescue check completed",
            state=state,
        )
        return result


    def get_status(self) -> dict[str, Any]:
        quarantine = self.list_quarantine_batches()
        repair_items, repair_diagnostics = self._repair_anomaly_candidates(
            include_shared=True,
        )
        disk = shutil.disk_usage(self.project_root)
        return {
            "ok": True,
            "version": self.VERSION,
            "project_root": str(self.project_root),
            "supported_scopes": ["global", "runtime", "sandbox"],
            "default_scope": "runtime",
            "quarantine_root": str(self.quarantine_root),
            "recovery_root": str(self.recovery_root),
            "business_storage": {
                "authority": "global-cleaner",
                "database": str(self.business_history.database_path),
            },
            "managed_backups": self.list_managed_backups(),
            "quarantine": quarantine,
            "quarantine_health": quarantine.get("health", {}),
            "history": list(reversed(self._history_records(limit=50))),
            "rules": {
                "schema_version": self.rules.get("schema_version"),
                "override_path": str(self.rules_override_path),
                "override_exists": self.rules_override_path.exists(),
                "directory_rule_count": len(self.rules.get("directory_rules", [])),
                "file_rule_count": len(self.rules.get("file_rules", [])),
                "warnings": self.rule_warnings,
            },
            "anomalies": {
                "repairable_count": len(repair_items),
                "diagnostic_count": len(repair_diagnostics),
                "items": repair_items,
                "diagnostics": repair_diagnostics,
            },
            "disk": {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
                "free_percent": round(disk.free / max(1, disk.total) * 100, 2),
            },
            "safety": {
                "preview_plan_required": True,
                "plan_signature": "HMAC-SHA256",
                "git_protection": True,
                "never_force_unlock": True,
                "transactional_quarantine": True,
                "permanent_delete": False,
                "sha256_recovery_manifests": True,
                "tombstones": True,
            },
            "capabilities": {
                "mutation_root": str(self.project_root),
                "read_only": [
                    "status",
                    "preview",
                    "storage-analysis",
                    "anomaly-diagnosis",
                ],
                "recoverable_mutation": [
                    "low-risk-cleanup",
                    "anomaly-repair",
                    "quarantine",
                    "restore",
                ],
                "maintenance_layers": [
                    "cleaner",
                    "shared",
                    "package",
                ],
                "forbidden": [
                    "outside-project",
                    "source-code-edit",
                    "git-tracked-file",
                    "force-unlock",
                    "process-termination",
                    "active-package-lock",
                    "current-dist",
                    "rollback-incomplete",
                    "dependency-directory",
                    "browser-profile",
                    "user-data",
                ],
            },
            "message": "project cleaner status ready",
        }

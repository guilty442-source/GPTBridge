from __future__ import annotations

import os
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable

from utils.cleanup import quarantine_path


EXCLUDED_DIRS = {
    ".git",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".GPTBridge_RuntimeSandbox",
    "__pycache__",
    "backups",
    "blob_storage",
    "build",
    "Cache",
    "Code Cache",
    "Cookies",
    "coverage",
    "Crashpad",
    "DawnCache",
    "dist-ui",
    "dist",
    "GPUCache",
    "IndexedDB",
    "Local Storage",
    "logs",
    "node_modules",
    "out",
    "playwright-report",
    "release",
    "runtime",
    "Service Worker",
    "ShaderCache",
    "Session Storage",
    "session data",
    "temp",
    "test-results",
    "tmp",
}

EXCLUDED_SUFFIXES = {
    ".7z",
    ".dll",
    ".exe",
    ".lock",
    ".log",
    ".map",
    ".pyd",
    ".tmp",
    ".zip",
}

MAX_FILE_BYTES = 20 * 1024 * 1024


class ScopedBackupStore:
    def __init__(self, source_dir: Path, backup_root: Path, max_records: int) -> None:
        self.source_dir = source_dir
        self.backup_root = backup_root
        self.max_records = max(1, max_records)
        self.backup_root.mkdir(parents=True, exist_ok=True)

    def create(self, label: str) -> dict[str, object]:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in label).strip("-")
        zip_path = self.backup_root / f"{timestamp}_{safe_label or 'backup'}.zip"
        partial_path = zip_path.with_name(
            f".{zip_path.name}.{uuid.uuid4().hex}.partial"
        )
        file_count = 0

        try:
            with zipfile.ZipFile(
                partial_path,
                "x",
                zipfile.ZIP_DEFLATED,
            ) as archive:
                for root, dirs, files in os.walk(self.source_dir):
                    root_path = Path(root)
                    rel_root = root_path.relative_to(self.source_dir)
                    if self._has_excluded_dir(rel_root):
                        dirs.clear()
                        continue

                    dirs[:] = [name for name in dirs if not self._is_excluded_name(name)]
                    for name in files:
                        path = root_path / name
                        rel_path = rel_root / name
                        if path.suffix.lower() in EXCLUDED_SUFFIXES:
                            continue
                        try:
                            if path.stat().st_size > MAX_FILE_BYTES:
                                continue
                            archive.write(path, rel_path)
                            file_count += 1
                        except OSError:
                            continue
            # Publish with an exclusive hard link. Unlike os.replace, this
            # cannot overwrite an independently created backup with the same
            # name. Removing the transaction-owned partial afterward leaves
            # the published link and all archive bytes intact.
            os.link(partial_path, zip_path)
        finally:
            # This unpublished partial belongs only to this backup transaction.
            try:
                partial_path.unlink(missing_ok=True)
            except OSError:
                pass

        retention = self.prune()
        return {
            "ok": True,
            "backup_file": str(zip_path),
            "backup_root": str(self.backup_root),
            "max_records": self.max_records,
            "file_count": file_count,
            "retention": retention,
            "records": self.records(),
        }

    def records(self) -> list[str]:
        return [str(path) for path in self._backup_files()]

    def restore_latest(self) -> dict[str, object]:
        latest = next(iter(self._backup_files()), None)
        if latest is None:
            return {
                "ok": False,
                "message": "no restorable backup record found",
                "backup_root": str(self.backup_root),
                "max_records": self.max_records,
                "records": [],
            }

        restored_files = 0
        recoveries: list[dict[str, object]] = []
        errors: list[dict[str, str]] = []
        source_root = self.source_dir.resolve()
        with zipfile.ZipFile(latest, "r") as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue

                target = (self.source_dir / member.filename).resolve()
                if source_root not in target.parents and target != source_root:
                    continue

                partial = target.with_name(
                    f".{target.name}.{uuid.uuid4().hex}.restore-partial"
                )
                try:
                    if target.exists():
                        recoveries.append(
                            quarantine_path(
                                target,
                                self.backup_root / "restore-recovery",
                                operation="scoped-backup-restore",
                                allowed_root=source_root,
                                original_path=member.filename,
                            )
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member, "r") as source, partial.open(
                        "xb"
                    ) as destination:
                        shutil.copyfileobj(source, destination)
                        destination.flush()
                        os.fsync(destination.fileno())
                    # Exclusive publication refuses to overwrite a target
                    # created concurrently after the recovery move.
                    os.link(partial, target)
                    restored_files += 1
                except (OSError, ValueError, zipfile.BadZipFile) as exc:
                    errors.append(
                        {"path": member.filename, "error": str(exc)}
                    )
                finally:
                    try:
                        partial.unlink(missing_ok=True)
                    except OSError:
                        pass

        return {
            "ok": not errors,
            "message": f"latest backup restored; files={restored_files}",
            "backup_file": str(latest),
            "backup_root": str(self.backup_root),
            "max_records": self.max_records,
            "file_count": restored_files,
            "recoveries": recoveries,
            "errors": errors,
            "permanently_deleted": 0,
            "records": self.records(),
        }

    def prune(self) -> dict[str, object]:
        """Delete published ZIP backups beyond the configured retention limit."""

        records = self._backup_files()
        overflow = records[self.max_records :]
        deleted_records: list[str] = []
        deleted_bytes = 0
        errors: list[dict[str, str]] = []
        for old_file in overflow:
            try:
                size = int(old_file.stat().st_size)
                old_file.unlink()
                deleted_records.append(str(old_file))
                deleted_bytes += size
            except OSError as exc:
                errors.append({"path": str(old_file), "error": str(exc)})

        remaining = self._backup_files()
        return {
            "pressure": len(remaining) > self.max_records,
            "configured_max_records": self.max_records,
            "record_count": len(remaining),
            "overflow_count": max(0, len(remaining) - self.max_records),
            "overflow_bytes": 0,
            "retained_records": [str(path) for path in remaining],
            "deleted_records": deleted_records,
            "deleted_bytes": deleted_bytes,
            "permanently_deleted": len(deleted_records),
            "errors": errors,
            "message": (
                f"backup retention pruned {len(deleted_records)} old record(s)"
                if deleted_records
                else "backup retention healthy"
            ),
        }

    def total_size_bytes(self) -> int:
        total = 0
        for path in self.backup_root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    def _backup_files(self) -> list[Path]:
        return sorted(self.backup_root.glob("*.zip"), key=lambda path: path.name, reverse=True)

    @staticmethod
    def _has_excluded_dir(path: Path) -> bool:
        return any(ScopedBackupStore._is_excluded_name(part) for part in path.parts)

    @staticmethod
    def _is_excluded_name(name: str) -> bool:
        normalized = name.lower()
        return any(normalized == item.lower() for item in EXCLUDED_DIRS)


def directory_size_bytes(paths: Iterable[Path], *, use_exclusions: bool = True) -> int:
    total = 0
    for base in paths:
        if not base.exists():
            continue
        if base.is_file():
            try:
                if not use_exclusions or base.suffix.lower() not in EXCLUDED_SUFFIXES:
                    total += base.stat().st_size
            except OSError:
                pass
            continue

        for root, dirs, files in os.walk(base):
            root_path = Path(root)
            if use_exclusions:
                dirs[:] = [name for name in dirs if not ScopedBackupStore._is_excluded_name(name)]

            for name in files:
                path = root_path / name
                if use_exclusions and path.suffix.lower() in EXCLUDED_SUFFIXES:
                    continue
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    return total

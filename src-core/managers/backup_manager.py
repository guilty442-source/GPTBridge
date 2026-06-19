from __future__ import annotations

import asyncio
import os
import shutil
import json
import zipfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from core_system.storage_paths import ensure_backup_layout, main_backup_root, project_root_from

# Constraints
MAX_BACKUP_COUNT = 3
MAX_TOTAL_SIZE_BYTES = 1024 * 1024 * 1024  # 1GB
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024    # 20MB

EXCLUDED_DIR_NAMES = frozenset({
    "node_modules",
    ".venv",
    ".GPTBridge_RuntimeSandbox",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist-ui",
    "backups",
    "__pycache__",
    "blob_storage",
    "runtime",
    "release",
    ".git",
    "Cache",
    "Code Cache",
    "Crashpad",
    "DawnCache",
    "GPUCache",
    "Service Worker",
    "ShaderCache",
    "IndexedDB",
    "Local Storage",
    "Session Storage",
    "logs",
    "coverage",
    "build",
    "out",
})

EXCLUDED_EXTENSIONS = frozenset({
    ".lock",
    ".map",
    ".exe",
    ".dll",
    ".pyd",
    ".zip",
    ".7z",
    ".log",
    ".tmp",
})


def _path_has_excluded_dir(rel_path: Path) -> bool:
    return any(part in EXCLUDED_DIR_NAMES for part in rel_path.parts)


def _is_permission_denied(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in (13, 5)  # EACCES, EPERM / WinError 5
    return False


def _warn_skip(path: Path | str, reason: str) -> None:
    print(f"[BackupManager] warning: skipped {path} ({reason})")


class BackupManager:
    def __init__(self, source_dir: Path, backup_root: Path | None = None, max_backup_count: int | None = None, logger: Any | None = None) -> None:
        self.source_dir = source_dir
        project_root = project_root_from(Path(__file__))
        ensure_backup_layout(project_root)
        self.backup_root = backup_root or main_backup_root(project_root)
        self.max_backup_count = max_backup_count or MAX_BACKUP_COUNT
        self.logger = logger
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self._last_backup_mtime: float = 0
        self._auto_backup_task: asyncio.Task | None = None

    def _walk_error(self, err: OSError) -> None:
        if _is_permission_denied(err):
            _warn_skip(err.filename or self.source_dir, "permission denied")
            return
        raise err

    def _get_source_max_mtime(self) -> float:
        max_mtime = 0.0
        for p in self.source_dir.rglob("*"):
            try:
                rel = p.relative_to(self.source_dir)
                if _path_has_excluded_dir(rel) or p.suffix.lower() in EXCLUDED_EXTENSIONS:
                    continue
                if p.is_file() and p.stat().st_size <= MAX_FILE_SIZE_BYTES:
                    mtime = p.stat().st_mtime
                    if mtime > max_mtime:
                        max_mtime = mtime
            except Exception:
                pass
        return max_mtime

    def _cleanup_old_backups(self) -> None:
        """Keep only the 3 most recent backups and ensure total size < 1GB."""
        try:
            backups = self.list_backups()  # Sorted newest first
            
            # 1. Limit by count
            if len(backups) > self.max_backup_count:
                for old in backups[self.max_backup_count:]:
                    try:
                        old.unlink()
                        if self.logger:
                            self.logger.write("core", "old backup removed (count limit)", {"path": str(old)})
                    except Exception as e:
                        print(f"[BackupManager] cleanup error (count) for {old}: {e}")
                backups = backups[:self.max_backup_count]

            # 2. Limit by total size
            total_size = sum(p.stat().st_size for p in backups if p.is_file())
            while total_size > MAX_TOTAL_SIZE_BYTES and backups:
                oldest = backups.pop()
                try:
                    sz = oldest.stat().st_size
                    oldest.unlink()
                    total_size -= sz
                    if self.logger:
                        self.logger.write("core", "old backup removed (size limit)", {"path": str(oldest)})
                    print(f"[BackupManager] total size limit exceeded, removed: {oldest.name}")
                except Exception as e:
                    print(f"[BackupManager] cleanup error (size) for {oldest}: {e}")
        except Exception as e:
            print(f"[BackupManager] cleanup failed: {e}")

    def create_backup(self, ignore_patterns: Sequence[str] | None = None) -> Path | None:
        if not self.source_dir.exists():
            return None

        current_mtime = self._get_source_max_mtime()
        if current_mtime == 0 or current_mtime <= self._last_backup_mtime:
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = self.backup_root / f"{timestamp}.zip"

        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(self.source_dir, onerror=self._walk_error):
                    root_path = Path(root)
                    rel_root = root_path.relative_to(self.source_dir)

                    if _path_has_excluded_dir(rel_root):
                        dirs.clear()
                        continue

                    dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
                    
                    if ignore_patterns:
                        ignored = set(shutil.ignore_patterns(*ignore_patterns)(root, dirs + files))
                        dirs[:] = [d for d in dirs if d not in ignored]
                        files = [f for f in files if f not in ignored]

                    for name in files:
                        src_file = root_path / name
                        rel_file = rel_root / name
                        if src_file.suffix.lower() in EXCLUDED_EXTENSIONS:
                            continue
                        
                        try:
                            fsize = src_file.stat().st_size
                            if fsize > MAX_FILE_SIZE_BYTES:
                                _warn_skip(src_file, f"size > 20MB ({fsize/1024/1024:.1f}MB)")
                                continue
                            zf.write(src_file, rel_file)
                        except (PermissionError, OSError) as exc:
                            if _is_permission_denied(exc):
                                _warn_skip(src_file, "permission denied")
                            else:
                                raise

            self._last_backup_mtime = current_mtime
            size_mb = zip_path.stat().st_size / (1024 * 1024)
            print(f"[BackupManager] backup created: {zip_path.name} ({size_mb:.2f} MB)")
            self._cleanup_old_backups()
            return zip_path
        except Exception as e:
            if zip_path.exists():
                zip_path.unlink()
            print(f"[BackupManager] create_backup failed: {e}")
            return None

    async def _backup_loop(self, interval_seconds: int) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await asyncio.to_thread(self.create_backup)
            except Exception as e:
                print(f"[BackupManager] loop error: {e}")

    async def start_auto_backup(self, interval_seconds: int = 1800) -> None:
        if self._auto_backup_task is None or self._auto_backup_task.done():
            self._auto_backup_task = asyncio.create_task(self._backup_loop(interval_seconds))

    async def stop_auto_backup(self) -> None:
        if self._auto_backup_task and not self._auto_backup_task.done():
            self._auto_backup_task.cancel()
            try:
                await self._auto_backup_task
            except asyncio.CancelledError:
                pass

    def create_snapshot(self, operation_reason: str = "Manual snapshot", modified_files: list[str] | None = None) -> Path | None:
        if not self.source_dir.exists():
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_dir = self.backup_root / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_file = snapshot_dir / f"snapshot_{timestamp}.json"

        file_list = []
        if modified_files is not None:
            file_list = modified_files
        else:
            for p in self.source_dir.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(self.source_dir)
                if _path_has_excluded_dir(rel):
                    continue
                file_list.append(str(rel))

        with open(snapshot_file, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": timestamp,
                "reason": operation_reason,
                "modified_files": file_list,
                "rollback_slot": None,
            }, f, indent=2)
        return snapshot_file

    def list_backups(self) -> list[Path]:
        if not self.backup_root.exists():
            return []

        # Return zip files sorted newest first
        return sorted(
            [p for p in self.backup_root.glob("*.zip")],
            key=lambda p: p.name,
            reverse=True,
        )

    def restore_backup(self, backup_zip: Path) -> Path | None:
        backup_zip = Path(backup_zip)
        if not backup_zip.exists():
            return None

        try:
            with zipfile.ZipFile(backup_zip, 'r') as zf:
                zf.extractall(self.source_dir)
            return backup_zip
        except Exception as e:
            print(f"[BackupManager] restore error: {e}")
            return None

    def restore_latest_backup(self) -> Path | None:
        backups = self.list_backups()
        if not backups:
            return None
        return self.restore_backup(backups[0])

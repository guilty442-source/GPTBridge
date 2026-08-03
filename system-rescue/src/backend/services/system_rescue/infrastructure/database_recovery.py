from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def database_integrity(path: Path, owner_tool_id: str = "") -> str:
    if owner_tool_id and owner_tool_id != "system-rescue":
        raise sqlite3.DatabaseError(
            "protected database verification belongs to the owner tool"
        )
    raw = path.read_bytes()
    if raw[:16] != b"SQLite format 3\x00":
        raise sqlite3.DatabaseError(
            "protected database verification belongs to the owner tool"
        )
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=3,
    )
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if not result or str(result[0]).casefold() != "ok":
        raise sqlite3.DatabaseError(str(result))
    return "ok"


class DatabaseRecoveryInspector:
    """Inspect only governed runtime databases and preserve corrupt evidence."""

    def __init__(self, project_root: Path, target_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.target_root = target_root.resolve()

    @staticmethod
    def sqlite_candidates(target_root: Path) -> list[Path]:
        candidates: set[Path] = set()
        for relative_root in (Path("runtime") / "state", Path("data") / "business"):
            root = target_root / relative_root
            if not root.is_dir():
                continue
            for pattern in ("*.sqlite", "*.sqlite3", "*.db"):
                candidates.update(path for path in root.rglob(pattern) if path.is_file())
        for name in ("system-channel.sqlite3", "ai-channel.sqlite3"):
            shared_database = target_root / "data" / name
            if shared_database.is_file():
                candidates.add(shared_database)
        return sorted(candidates)

    def inspect(self) -> dict[str, Any]:
        recovery_root = self.target_root / "runtime" / "recovery" / "database"
        recovery_root.mkdir(parents=True, exist_ok=True)
        if not _inside(recovery_root, self.target_root):
            raise PermissionError("PERMISSION_DENIED")

        checked: list[str] = []
        preserved: list[str] = []
        extraction_paths: list[str] = []
        errors: list[str] = []
        for database in self.sqlite_candidates(self.target_root):
            if not _inside(database, self.target_root):
                continue
            relative_project = database.relative_to(self.project_root).as_posix()
            try:
                database_integrity(database)
                checked.append(relative_project)
            except (OSError, sqlite3.DatabaseError) as error:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                destination = recovery_root / f"{database.name}.{stamp}.corrupt"
                try:
                    shutil.copy2(database, destination)
                    preserved.append(destination.relative_to(self.target_root).as_posix())
                except OSError as preserve_error:
                    errors.append(
                        f"{relative_project}: preserve failed "
                        f"({type(preserve_error).__name__})"
                    )
                extraction_paths.append(relative_project)
                errors.append(f"{relative_project}: {type(error).__name__}")
        return {
            "checked_databases": checked,
            "preserved_databases": preserved,
            "backup_extract_paths": extraction_paths,
            "backup_extract_required": bool(extraction_paths),
            "database_errors": errors,
        }


__all__ = ["DatabaseRecoveryInspector", "database_integrity"]

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json

REPAIR_VERSION: str = "1.0.0"


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_integrity(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:16] != b"SQLite format 3\x00":
        raise sqlite3.DatabaseError("not a valid SQLite file")
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3
    )
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if not result or str(result[0]).casefold() != "ok":
        raise sqlite3.DatabaseError(str(result))
    return "ok"


def sqlite_candidates(tool_root: Path) -> list[Path]:
    candidates: set[Path] = set()
    for relative_root in (Path("runtime") / "state", Path("data") / "business"):
        root = tool_root / relative_root
        if not root.is_dir():
            continue
        for pattern in ("*.sqlite", "*.sqlite3", "*.db"):
            candidates.update(
                path for path in root.rglob(pattern) if path.is_file()
            )
    for name in ("system-channel.sqlite3", "ai-channel.sqlite3"):
        shared_database = tool_root / "data" / name
        if shared_database.is_file():
            candidates.add(shared_database)
    return sorted(candidates)


def pycache_candidates(tool_root: Path) -> list[Path]:
    candidates: list[Path] = []
    source_root = tool_root / "src"
    if not source_root.is_dir():
        return candidates
    for pycache in source_root.rglob("__pycache__"):
        if pycache.is_dir():
            candidates.append(pycache)
    return sorted(candidates)


@dataclass(frozen=True)
class LocalRepairResult:
    ok: bool
    operation: str
    authority: str
    version: str
    tool_id: str
    checked_databases: list[str]
    preserved_databases: list[str]
    backup_extract_required: bool
    database_errors: list[str]
    cleared_pycache_dirs: list[str]
    pycache_errors: list[str]
    errors: list[str]
    started_at: str
    completed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self)}


class ToolLocalRepair:
    """Bounded, tool-root-local self repair.

    Every operation is forced to resolve within ``tool_root`` so a tool can
    only ever repair its own databases and bytecode cache. It never repairs or
    inspects any other module and never touches governance-rule or main-system.
    """

    def __init__(
        self,
        tool_id: str,
        tool_root: Path | str,
        *,
        clear_pycache: bool = True,
    ) -> None:
        self.tool_id = tool_id
        self.tool_root = Path(tool_root).resolve()
        self.clear_pycache = clear_pycache

    def _quarantine_root(self) -> Path:
        recovery_root = self.tool_root / "runtime" / "recovery" / "database"
        recovery_root.mkdir(parents=True, exist_ok=True)
        if not _inside(recovery_root, self.tool_root):
            raise PermissionError("PERMISSION_DENIED")
        return recovery_root

    def repair(self) -> LocalRepairResult:
        started_at = _iso_now()
        checked: list[str] = []
        preserved: list[str] = []
        extraction_paths: list[str] = []
        database_errors: list[str] = []
        errors: list[str] = []

        recovery_root = self._quarantine_root()
        for database in sqlite_candidates(self.tool_root):
            if not _inside(database, self.tool_root):
                continue
            relative = database.relative_to(self.tool_root).as_posix()
            try:
                database_integrity(database)
                checked.append(relative)
            except (OSError, sqlite3.DatabaseError) as error:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                destination = recovery_root / f"{database.name}.{stamp}.corrupt"
                try:
                    shutil.copy2(database, destination)
                    preserved.append(
                        destination.relative_to(self.tool_root).as_posix()
                    )
                except OSError as preserve_error:
                    errors.append(
                        f"{relative}: preserve failed "
                        f"({type(preserve_error).__name__})"
                    )
                extraction_paths.append(relative)
                database_errors.append(f"{relative}: {type(error).__name__}")

        cleared_pycache: list[str] = []
        pycache_errors: list[str] = []
        if self.clear_pycache:
            for pycache in pycache_candidates(self.tool_root):
                if not _inside(pycache, self.tool_root):
                    continue
                relative = pycache.relative_to(self.tool_root).as_posix()
                try:
                    shutil.rmtree(pycache, ignore_errors=False)
                    cleared_pycache.append(relative)
                except OSError as error:
                    pycache_errors.append(f"{relative}: {type(error).__name__}")

        return LocalRepairResult(
            ok=not database_errors and not errors and not pycache_errors,
            operation="local-self-repair",
            authority="tool-local",
            version=REPAIR_VERSION,
            tool_id=self.tool_id,
            checked_databases=checked,
            preserved_databases=preserved,
            backup_extract_required=bool(extraction_paths),
            database_errors=database_errors,
            cleared_pycache_dirs=cleared_pycache,
            pycache_errors=pycache_errors,
            errors=errors,
            started_at=started_at,
            completed_at=_iso_now(),
        )


def run_local_self_repair(
    tool_id: str,
    tool_root: Path | str,
    *,
    clear_pycache: bool = True,
    ledger: Path | None = None,
) -> dict[str, Any]:
    """Convenience runner that also writes a per-tool repair ledger if given."""
    result = ToolLocalRepair(
        tool_id, tool_root, clear_pycache=clear_pycache
    ).repair()
    payload = result.as_dict()
    if ledger is not None:
        ledger_root = Path(ledger).resolve()
        ledger_root.mkdir(parents=True, exist_ok=True)
        record_path = ledger_root / "automatic-repair.sqlite3"
        connection = sqlite3.connect(record_path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS self_repair_runs ("
                "run_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, "
                "started_at TEXT NOT NULL, completed_at TEXT NOT NULL, "
                "ok INTEGER NOT NULL, detail_json TEXT NOT NULL)"
            )
            run_id = f"{result.started_at}-{result.tool_id}"
            connection.execute(
                "INSERT INTO self_repair_runs "
                "(run_id, tool_id, started_at, completed_at, ok, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    result.tool_id,
                    result.started_at,
                    result.completed_at,
                    int(bool(result.ok)),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        payload["database"] = str(record_path)
    return payload


__all__ = [
    "LocalRepairResult",
    "ToolLocalRepair",
    "database_integrity",
    "pycache_candidates",
    "run_local_self_repair",
    "sqlite_candidates",
]

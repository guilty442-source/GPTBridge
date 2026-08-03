from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

CENTRAL_REPAIR_VERSION: Final[str] = "1.0.0"

PACKAGE_REBUILD_FAILURES: Final[frozenset[str]] = frozenset(
    {
        "EXECUTABLE_MISSING",
        "PACKAGE_UNVERIFIED",
        "STALE_TOOL_PACKAGE",
        "INCOMPATIBLE_TOOL_RUNTIME",
        "PROCESS_START_FAILED",
        "SOURCE_UI_UNAVAILABLE",
        "SOURCE_RUNTIME_NOT_READY",
        "TOOL_VERSION_MISMATCH",
        "BACKEND_CONNECTION_FAILED",
        "FRONTEND_BACKEND_DISCONNECTED",
        "MODEL_RUNTIME_NOT_READY",
        "COMMAND_EXECUTION_FAILED",
        "STREAM_CHANNEL_FAILED",
    }
)


@dataclass(frozen=True)
class RepairPlan:
    failure_code: str
    inspect_databases: bool
    rebuild_executable: bool

    @property
    def actions(self) -> tuple[str, ...]:
        actions = ("inspect-owned-databases",) if self.inspect_databases else ()
        if self.rebuild_executable:
            actions = (*actions, "rebuild-tool-executable")
        return actions

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "actions": list(self.actions)}


def plan_repair(failure_code: str) -> RepairPlan:
    normalized = str(failure_code or "TOOL_START_FAILED").strip().upper()[:128]
    return RepairPlan(
        failure_code=normalized or "TOOL_START_FAILED",
        inspect_databases=True,
        rebuild_executable=normalized in PACKAGE_REBUILD_FAILURES,
    )


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


class DatabaseRecoveryInspector:
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
                candidates.update(
                    path for path in root.rglob(pattern) if path.is_file()
                )
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
                    preserved.append(
                        destination.relative_to(self.target_root).as_posix()
                    )
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


class RepairRunStore:
    def __init__(self, database_root: Path) -> None:
        self.database_root = database_root.resolve()

    def _connect(self, target_id: str) -> tuple[sqlite3.Connection, Path]:
        owner_root = self.database_root / target_id
        owner_root.mkdir(parents=True, exist_ok=True)
        path = owner_root / "automatic-repair.sqlite3"
        connection = sqlite3.connect(path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS repair_runs ("
            "run_id TEXT PRIMARY KEY, target_tool_id TEXT NOT NULL, "
            "started_at TEXT NOT NULL, completed_at TEXT NOT NULL, "
            "failure_code TEXT NOT NULL, ok INTEGER NOT NULL, "
            "detail_json TEXT NOT NULL)"
        )
        return connection, path

    def record(self, target_id: str, result: dict[str, Any]) -> Path:
        connection, path = self._connect(target_id)
        try:
            connection.execute(
                "INSERT INTO repair_runs "
                "(run_id, target_tool_id, started_at, completed_at, "
                "failure_code, ok, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    result["run_id"],
                    target_id,
                    result["started_at"],
                    result["completed_at"],
                    result["failure_code"],
                    int(bool(result["ok"])),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return path


class CentralRepairService:
    """Central automatic-repair service, integrated into main-system."""

    VERSION = CENTRAL_REPAIR_VERSION

    def __init__(self, project_root: Path, repair_data_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.repair_data_root = repair_data_root.resolve()
        self.store = RepairRunStore(self.repair_data_root)

    def _validate_target(self, target_tool_id: str) -> tuple[str, Path]:
        target_id = str(target_tool_id or "").strip()
        if (
            not target_id
            or any(
                c not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                for c in target_id
            )
            or target_id in {"governance-rule", "main-system"}
        ):
            raise PermissionError("PERMISSION_DENIED")
        target_root = (self.project_root / target_id).resolve()
        if target_id == "shared-layer":
            target_root = (self.project_root / "shared-layer").resolve()
            if not (target_root / "src" / "shared_layer" / "store.py").is_file():
                raise PermissionError("PERMISSION_DENIED")
        else:
            manifest_path = target_root / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise PermissionError("PERMISSION_DENIED") from error
            if str(manifest.get("id") or "") != target_id:
                raise PermissionError("PERMISSION_DENIED")
        if not _inside(target_root, self.project_root):
            raise PermissionError("PERMISSION_DENIED")
        return target_id, target_root

    def repair_tool(
        self,
        target_tool_id: str,
        failure_code: str,
        *,
        package_rebuilder: Any | None = None,
    ) -> dict[str, Any]:
        target_id, target_root = self._validate_target(target_tool_id)
        plan = plan_repair(failure_code)
        run_id = uuid.uuid4().hex
        started_at = _iso_now()
        inspection = DatabaseRecoveryInspector(
            self.project_root, target_root
        ).inspect()
        errors = list(inspection["database_errors"])
        package_repair: dict[str, Any] = {
            "triggered": False,
            "owner": "main-system",
            "reason": "PACKAGE_REBUILD_NOT_REQUIRED",
        }
        executed_actions: list[str] = ["inspect-owned-databases"]
        if plan.rebuild_executable:
            if package_rebuilder is None:
                package_repair = {
                    "triggered": True,
                    "ok": False,
                    "owner": "main-system",
                    "error_code": "PACKAGE_REBUILDER_UNAVAILABLE",
                }
            else:
                try:
                    package_repair = {"triggered": True, **package_rebuilder(target_id)}
                except Exception as error:
                    package_repair = {
                        "triggered": True,
                        "ok": False,
                        "owner": "main-system",
                        "error_code": "PACKAGE_REBUILD_FAILED",
                        "message": str(error),
                    }
            executed_actions.append("rebuild-tool-executable")
            if package_repair.get("ok") is not True:
                errors.append(
                    str(package_repair.get("error_code") or "PACKAGE_REBUILD_FAILED")
                )

        result: dict[str, Any] = {
            "ok": not errors,
            "operation": "central-automatic-repair",
            "authority": "main-system",
            "version": self.VERSION,
            "run_id": run_id,
            "target_tool_id": target_id,
            "failure_code": plan.failure_code,
            "repair_plan": plan.as_dict(),
            "executed_actions": executed_actions,
            **inspection,
            "package_repair": package_repair,
            "direct_backup_access": False,
            "errors": errors,
            "started_at": started_at,
            "completed_at": _iso_now(),
        }
        result["database"] = str(self.store.record(target_id, result))
        return result


__all__ = [
    "CentralRepairService",
    "DatabaseRecoveryInspector",
    "RepairPlan",
    "RepairRunStore",
    "plan_repair",
    "database_integrity",
]

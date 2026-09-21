from __future__ import annotations

import json
import shutil
import sqlite3
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_integrity(path: Path, owner: str | None = None) -> str:
    raw = path.read_bytes()
    if raw[:1] == b"{" and owner is not None:
        raise sqlite3.DatabaseError("protected database verification belongs to the owner tool")
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


def classify_trash_modules(project_root: Path) -> list[dict[str, str | bool]]:
    """Return retired modules assigned to the governed ``trash`` label.

    This is inspection-only.  Classification never authorizes deletion,
    movement, reset, or reactivation of a retired module.
    """
    registry_path = (
        project_root
        / "governance_rule"
        / "execution"
        / "audit"
        / "architecture_registry.json"
    )
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    labels = registry.get("module_labels")
    trash_ids = labels.get("trash") if isinstance(labels, dict) else None
    components = registry.get("components")
    if not isinstance(trash_ids, list) or not isinstance(components, list):
        return []
    by_id = {
        str(component.get("component_id") or ""): component
        for component in components
        if isinstance(component, dict)
    }
    classified: list[dict[str, str | bool]] = []
    for raw_id in trash_ids:
        component_id = str(raw_id or "").strip()
        component = by_id.get(component_id)
        if not component or component.get("lifecycle") != "retired":
            continue
        classified.append(
            {
                "component_id": component_id,
                "physical_path": str(component.get("physical_path") or ""),
                "label": "trash",
                "lifecycle": "retired",
                "classification_source": "architecture-registry",
                "deletion_authorized": False,
            }
        )
    return classified


ARCHITECTURE_REGISTRY_RELATIVE = (
    Path("governance_rule") / "execution" / "audit" / "architecture_registry.json"
)


def _normalized_relative(value: object) -> str:
    return str(value or "").replace("\\", "/").strip("/").casefold()


def _registered_component_paths(project_root: Path) -> set[str]:
    registry_path = project_root / ARCHITECTURE_REGISTRY_RELATIVE
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    components = registry.get("components")
    if not isinstance(components, list):
        return set()
    return {
        _normalized_relative(component.get("physical_path"))
        for component in components
        if isinstance(component, dict) and component.get("physical_path")
    }


def _root_has_only_empty_directories(root: Path) -> bool:
    for child in root.rglob("*"):
        if child.is_symlink() or child.is_file():
            return False
    return True


def classify_orphan_component_roots(
    project_root: Path,
) -> list[dict[str, str | bool]]:
    """Return unregistered physical roots under the canonical tool container.

    Classification-only: detection never authorizes deletion, movement or
    reuse.  A root is an orphan candidate when it is not declared by the
    architecture registry and carries no manifest; empty roots are marked
    ``garbage`` and roots with residue are marked ``review`` (they may be
    retired evidence or data that must be preserved).
    """
    project_root = Path(project_root)
    tools_root = project_root / "Standalone tools"
    if not tools_root.is_dir():
        return []
    registered = _registered_component_paths(project_root)
    candidates: list[dict[str, str | bool]] = []
    for child in sorted(tools_root.iterdir(), key=lambda item: item.name.casefold()):
        if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
            continue
        relative = f"Standalone tools/{child.name}"
        if _normalized_relative(relative) in registered:
            continue
        has_manifest = (child / "manifest.json").is_file()
        empty = _root_has_only_empty_directories(child)
        if has_manifest:
            label, reason = "review", "unregistered manifest-bearing root"
        elif empty:
            label, reason = "garbage", "unregistered empty root"
        else:
            label, reason = "review", "unregistered root with residue"
        candidates.append(
            {
                "component_id": child.name,
                "physical_path": relative,
                "label": label,
                "reason": reason,
                "has_manifest": has_manifest,
                "empty": empty,
                "classification_source": "architecture-registry",
                "removal_authorized": False,
            }
        )
    return candidates


def schedule_trash_cleanup(
    project_root: Path,
    classified: list[dict[str, str | bool]],
    orphan_candidates: list[dict[str, str | bool]] | None = None,
) -> dict[str, Any]:
    """Publish trash and orphan candidates to the automatic cleanup schedule.

    The assistant only schedules.  The cleanup service must revalidate the
    current registry classification before any later destructive action, and
    no candidate entry ever authorizes deletion by itself.
    """
    queue_path = (
        project_root
        / "main-system"
        / "runtime"
        / "state"
        / "trash-cleanup-schedule.json"
    )
    candidate_ids = sorted(
        str(item.get("component_id") or "")
        for item in classified
        if item.get("component_id")
    )
    orphan_candidates = list(orphan_candidates or [])
    orphan_ids = sorted(
        str(item.get("component_id") or "")
        for item in orphan_candidates
        if item.get("component_id")
    )
    orphan_labels = {
        str(item.get("component_id") or ""): str(item.get("label") or "")
        for item in orphan_candidates
        if item.get("component_id")
    }
    previous: dict[str, Any] = {}
    try:
        loaded = json.loads(queue_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            previous = loaded
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    unchanged = (
        previous.get("status") == "scheduled"
        and previous.get("candidate_ids") == candidate_ids
        and previous.get("orphan_candidate_ids") == orphan_ids
    )
    payload = {
        "schedule_id": previous.get("schedule_id") if unchanged else f"trash-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}",
        "status": "scheduled" if candidate_ids or orphan_ids else "empty",
        "source": "xingcheng-assistant-inspection",
        "executor": "automatic-cleanup-scheduler",
        "candidate_ids": candidate_ids,
        "orphan_candidate_ids": orphan_ids,
        "orphan_labels": orphan_labels,
        "scheduled_at": previous.get("scheduled_at") if unchanged else _iso_now(),
        "revalidation_required": True,
        "direct_delete": False,
    }
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = queue_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, queue_path)
    return payload


def _inspect_database(
    database: Path,
    *,
    project_root: Path,
    target_root: Path,
    recovery_root: Path,
) -> tuple[str, str, str, str]:
    relative_project = database.relative_to(project_root).as_posix()
    try:
        database_integrity(database)
        return (relative_project, "checked", "", "")
    except (OSError, sqlite3.DatabaseError) as error:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = recovery_root / f"{database.name}.{stamp}.corrupt"
        try:
            shutil.copy2(database, destination)
            preserved = destination.relative_to(target_root).as_posix()
            preserve_error = ""
        except OSError as preserve_exception:
            preserved = ""
            preserve_error = (
                f"{relative_project}: preserve failed "
                f"({type(preserve_exception).__name__})"
            )
        return (
            relative_project,
            "corrupt",
            preserved,
            "; ".join(
                item
                for item in (
                    preserve_error,
                    f"{relative_project}: {type(error).__name__}",
                )
                if item
            ),
        )


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
        databases = [
            database
            for database in self.sqlite_candidates(self.target_root)
            if _inside(database, self.target_root)
        ]
        max_workers = min(4, len(databases)) or 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(
                executor.map(
                    lambda database: _inspect_database(
                        database,
                        project_root=self.project_root,
                        target_root=self.target_root,
                        recovery_root=recovery_root,
                    ),
                    databases,
                )
            )
        for relative_project, status, preserved_path, error in sorted(results):
            if status == "checked":
                checked.append(relative_project)
                continue
            extraction_paths.append(relative_project)
            if preserved_path:
                preserved.append(preserved_path)
            if error:
                errors.append(error)
        trash_modules = classify_trash_modules(self.project_root)
        orphan_candidates = classify_orphan_component_roots(self.project_root)
        trash_schedule = schedule_trash_cleanup(
            self.project_root, trash_modules, orphan_candidates
        )
        return {
            "checked_databases": checked,
            "preserved_databases": preserved,
            "backup_extract_paths": extraction_paths,
            "backup_extract_required": bool(extraction_paths),
            "database_errors": errors,
            "trash_classification": trash_modules,
            "trash_count": len(trash_modules),
            "trash_action": "classification-only",
            "orphan_classification": orphan_candidates,
            "orphan_count": len(orphan_candidates),
            "orphan_action": "classification-only",
            "trash_cleanup_schedule": trash_schedule,
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

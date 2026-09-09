from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from .repair_learning import (
    ErrorSignature,
    RepairLearner,
    RepairLearningStore,
    RepairOutcome,
    _normalize_error_signature,
)

CENTRAL_REPAIR_VERSION: Final[str] = "1.2.0"

SOURCE_SELF_REPAIR_FAILURES: Final[frozenset[str]] = frozenset(
    {
        "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED",
        "BACKEND_CONNECTION_FAILED",
        "FRONTEND_BACKEND_DISCONNECTED",
        "PROCESS_START_FAILED",
    }
)

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
    repair_main_system_source: bool = False

    @property
    def actions(self) -> tuple[str, ...]:
        actions = ("inspect-owned-databases",) if self.inspect_databases else ()
        if self.rebuild_executable:
            actions = (*actions, "rebuild-tool-executable")
        if self.repair_main_system_source:
            actions = (*actions, "repair-main-system-source")
        return actions

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "actions": list(self.actions)}


def plan_repair(failure_code: str) -> RepairPlan:
    normalized = str(failure_code or "TOOL_START_FAILED").strip().upper()[:128]
    return RepairPlan(
        failure_code=normalized or "TOOL_START_FAILED",
        inspect_databases=True,
        rebuild_executable=normalized in PACKAGE_REBUILD_FAILURES,
        repair_main_system_source=normalized in SOURCE_SELF_REPAIR_FAILURES,
    )


REPAIR_RECIPES: Final[tuple[dict[str, Any], ...]] = (
    {
        "recipe_id": "main-system-python-source-syntax",
        "name": "Backend Python source indentation/syntax self-repair",
        "failure_signatures": (
            "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED",
            "IndentationError",
            "TabError",
            "SyntaxError",
        ),
        "owner": "main-system",
        "remedy": (
            "compile self-check over all backend src roots then deterministic "
            "column-0 indentation recovery via tasks.source_repair "
            "(skips files with uncommitted git changes)"
        ),
        "verification": (
            "full source compile passes across all backend src roots; "
            "repair recorded in automatic-repair store; files with "
            "uncommitted git changes are skipped"
        ),
        "automatic": True,
        "runtime_only": False,
    },
    {
        "recipe_id": "tool-package-rebuild",
        "name": "Tool package rebuild on startup/runtime failure",
        "failure_signatures": tuple(sorted(PACKAGE_REBUILD_FAILURES)),
        "owner": "main-system",
        "remedy": "plan_repair rebuild-tool-executable via governed package rebuilder",
        "verification": "owned databases inspected; rebuilt executable starts",
        "automatic": True,
        "runtime_only": True,
    },
    {
        "recipe_id": "backend-exit-before-health",
        "name": "Backend exits before health ready",
        "failure_signatures": ("backend exited before health ready",),
        "owner": "main-system",
        "remedy": "governance-authorized re-spawn with bounded startup/autonomous recovery in launcher",
        "verification": "health probe returns ready with matching workspace instance id",
        "automatic": True,
        "runtime_only": True,
    },
    {
        "recipe_id": "frontend-backend-disconnected",
        "name": "Frontend-backend WebSocket disconnection repair",
        "failure_signatures": ("FRONTEND_BACKEND_DISCONNECTED",),
        "owner": "main-system",
        "remedy": (
            "connection watchdog detects persistent disconnection; "
            "frontend auto-reconnects (3 attempts) then triggers "
            "app:restart-backend via Electron IPC; boot_core restarts "
            "backend; learning store records the outage pattern"
        ),
        "verification": (
            "backend /health returns 200 and frontend WebSocket "
            "reconnects within probe interval"
        ),
        "automatic": True,
        "runtime_only": True,
    },
    {
        "recipe_id": "governance-codex-tamper",
        "name": "Governance codex file tamper detection",
        "failure_signatures": ("PermissionError", "AuthorityIntegrityGuard.verify"),
        "owner": "governance-rule",
        "remedy": (
            "fail-closed denial; restore codex files from trusted backup and "
            "re-verify manifest"
        ),
        "verification": "governance:audit and runtime integrity report healthy",
        "automatic": False,
        "runtime_only": False,
    },
)


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
    """Central automatic-repair service, integrated into main-system.

    Supports self-upgrading repair knowledge:
    - Every repair outcome is recorded as an error signature + remedy pair.
    - The RepairLearner analyzes history and auto-promotes recurring
      error→remedy patterns into learned recipes.
    - Learned recipes are merged into the knowledge base alongside the
      static REPAIR_RECIPES, making them available for future dispatch.
    """

    VERSION = CENTRAL_REPAIR_VERSION

    def __init__(self, project_root: Path, repair_data_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.repair_data_root = repair_data_root.resolve()
        self.store = RepairRunStore(self.repair_data_root)
        self.learning_store = RepairLearningStore(self.repair_data_root)
        self.learner = RepairLearner(self.learning_store)

    def _knowledge_file(self) -> Path:
        return self.repair_data_root / "knowledge" / "recipes.json"

    def known_recipes(self) -> list[dict[str, Any]]:
        knowledge_file = self._knowledge_file()
        recorded: list[dict[str, Any]] = []
        try:
            recorded = json.loads(knowledge_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            recorded = []
        if not isinstance(recorded, list):
            recorded = []
        merged: dict[str, dict[str, Any]] = {
            recipe["recipe_id"]: dict(recipe) for recipe in REPAIR_RECIPES
        }
        for recipe in recorded:
            if isinstance(recipe, dict) and recipe.get("recipe_id"):
                merged[str(recipe["recipe_id"])] = {**merged.get(str(recipe["recipe_id"]), {}), **recipe}
        # Merge learned recipes from the learning store.
        for learned in self.learning_store.get_learned_recipes():
            rid = str(learned.get("recipe_id") or "")
            if rid:
                merged[rid] = {**merged.get(rid, {}), **learned, "source": "learned"}
        recipes = list(merged.values())
        try:
            knowledge_file.parent.mkdir(parents=True, exist_ok=True)
            knowledge_file.write_text(
                json.dumps(recipes, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
        return recipes

    def record_recipe(self, recipe: dict[str, Any]) -> dict[str, Any]:
        knowledge_file = self._knowledge_file()
        recipes = self.known_recipes()
        recipe_id = str(recipe.get("recipe_id") or "")
        if not recipe_id:
            return {"ok": False, "error": "recipe_id required"}
        for index, existing in enumerate(recipes):
            if existing.get("recipe_id") == recipe_id:
                recipes[index] = {**existing, **recipe}
                break
        else:
            recipes.append(recipe)
        try:
            knowledge_file.parent.mkdir(parents=True, exist_ok=True)
            knowledge_file.write_text(
                json.dumps(recipes, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as error:
            return {"ok": False, "error": error.__class__.__name__}
        return {"ok": True, "recipe_id": recipe_id, "recipes": recipes}

    def knowledge_summary(self) -> dict[str, Any]:
        recipes = self.known_recipes()
        analysis = self.learner.analyze_history()
        return {
            "recipe_count": len(recipes),
            "automatic_recipes": sum(
                1 for recipe in recipes if recipe.get("automatic") is True
            ),
            "learned_recipe_count": analysis.get("learned_recipes", 0),
            "total_error_types": analysis.get("total_error_types", 0),
            "recurring_errors": analysis.get("recurring_errors", 0),
            "recipes": [
                {
                    "recipe_id": recipe.get("recipe_id"),
                    "name": recipe.get("name"),
                    "automatic": recipe.get("automatic"),
                    "owner": recipe.get("owner"),
                    "source": recipe.get("source", "static"),
                }
                for recipe in recipes
            ],
        }

    def status(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "enabled": True,
            "delegation": "governed-executor-only",
            "source_self_repair": True,
            "self_upgrading": True,
            "learning_enabled": True,
            "knowledge_base": self.knowledge_summary(),
        }

    def self_repair_main_system_sources(self) -> dict[str, Any]:
        from .source_repair import self_repair_sources

        report = self_repair_sources(self.project_root)
        # Learn from each repaired file and each error.
        for problem in report.get("problems", []):
            self._learn_from_problem(problem, report)
        for repaired in report.get("repaired_files", []):
            self._learn_from_repair(repaired, report)
        return {"repair_service": self.VERSION, **report}

    def _learn_from_problem(self, problem: dict[str, Any], report: dict[str, Any]) -> None:
        """Record an error signature from a detected problem."""
        try:
            error_class = str(problem.get("error") or "Unknown")
            message = str(problem.get("message") or "")
            file_path = str(problem.get("file") or "")
            sig = ErrorSignature(
                signature_hash=_normalize_error_signature(
                    error_class, message, file_path=file_path
                ),
                error_class=error_class,
                message_pattern=message[:200],
                failure_code=str(report.get("failure_code") or "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED"),
                file_context=file_path,
                target_tool_id="main-system",
            )
            remedy = "indentation-repair" if problem.get("indentation_family") else "no-remedy"
            outcome = RepairOutcome(
                run_id=str(report.get("run_id") or uuid.uuid4().hex),
                signature_hash=sig.signature_hash,
                remedy=remedy,
                ok=remedy != "no-remedy",
                detail={"file": file_path, "error_class": error_class},
            )
            self.learner.learn_from_outcome(sig, outcome)
        except Exception:
            pass  # Learning is best-effort; never block repair.

    def _learn_from_repair(self, repaired: dict[str, Any], report: dict[str, Any]) -> None:
        """Record a successful repair outcome for learning."""
        try:
            file_path = str(repaired.get("file") or "")
            error_class = "IndentationError"  # source_repair only does indentation
            sig = ErrorSignature(
                signature_hash=_normalize_error_signature(error_class, "indentation", file_path=file_path),
                error_class=error_class,
                message_pattern="indentation",
                failure_code=str(report.get("failure_code") or "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED"),
                file_context=file_path,
                target_tool_id="main-system",
            )
            outcome = RepairOutcome(
                run_id=str(report.get("run_id") or uuid.uuid4().hex),
                signature_hash=sig.signature_hash,
                remedy="indentation-repair",
                ok=True,
                detail={"file": file_path, "repaired_lines": repaired.get("repaired_lines", [])},
            )
            self.learner.learn_from_outcome(sig, outcome)
        except Exception:
            pass

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
        # Learn from this repair outcome.
        self._learn_from_tool_repair(target_id, plan.failure_code, result)
        return result

    def _learn_from_tool_repair(
        self, target_id: str, failure_code: str, result: dict[str, Any]
    ) -> None:
        """Record a tool repair outcome for learning."""
        try:
            ok = bool(result.get("ok"))
            remedy = ",".join(result.get("executed_actions", []))
            sig = ErrorSignature(
                signature_hash=_normalize_error_signature(
                    failure_code, remedy, file_path=target_id
                ),
                error_class=failure_code,
                message_pattern=remedy[:200],
                failure_code=failure_code,
                file_context=target_id,
                target_tool_id=target_id,
            )
            outcome = RepairOutcome(
                run_id=str(result.get("run_id") or uuid.uuid4().hex),
                signature_hash=sig.signature_hash,
                remedy=remedy,
                ok=ok,
                detail={"target": target_id, "actions": result.get("executed_actions", [])},
            )
            self.learner.learn_from_outcome(sig, outcome)
        except Exception:
            pass  # Learning is best-effort.

    def suggest_remedy_for_error(
        self, error_class: str, message: str, *, file_path: str = ""
    ) -> dict[str, Any]:
        """Look up the best known remedy for an error signature."""
        sig = ErrorSignature(
            signature_hash=_normalize_error_signature(
                error_class, message, file_path=file_path
            ),
            error_class=error_class,
            message_pattern=message[:200],
            failure_code="UNKNOWN",
            file_context=file_path,
        )
        return self.learner.suggest_remedy(sig)

    def record_connection_outcome(
        self,
        failure_code: str,
        from_state: str,
        to_state: str,
        *,
        remedy: str,
        ok: bool,
        run_id: str = "",
    ) -> None:
        """Record a connection/sync failure outcome with a consistent signature.

        The signature is derived from (failure_code, from_state->to_state,
        "ipc/connection") so that record and lookup use the same components.
        This closes the learning loop for connection failures: the same
        signature used to record an outcome is used to look up suggestions.
        """
        try:
            message = f"{from_state}->{to_state}"
            sig = ErrorSignature(
                signature_hash=_normalize_error_signature(
                    failure_code, message, file_path="ipc/connection",
                ),
                error_class=failure_code,
                message_pattern=message,
                failure_code=failure_code,
                file_context="ipc/connection",
                target_tool_id="main-system",
            )
            outcome = RepairOutcome(
                run_id=run_id or uuid.uuid4().hex,
                signature_hash=sig.signature_hash,
                remedy=remedy,
                ok=ok,
                detail={"from_state": from_state, "to_state": to_state},
            )
            self.learner.learn_from_outcome(sig, outcome)
        except Exception:
            pass  # Learning is best-effort.

    def suggest_connection_remedy(
        self, failure_code: str, from_state: str, to_state: str
    ) -> dict[str, Any]:
        """Look up the best known remedy for a connection failure.

        Uses the same signature components as ``record_connection_outcome``
        so the learning loop is closed: recorded outcomes are discoverable.
        """
        message = f"{from_state}->{to_state}"
        sig = ErrorSignature(
            signature_hash=_normalize_error_signature(
                failure_code, message, file_path="ipc/connection",
            ),
            error_class=failure_code,
            message_pattern=message,
            failure_code=failure_code,
            file_context="ipc/connection",
            target_tool_id="main-system",
        )
        return self.learner.suggest_remedy(sig)

    def learning_report(self) -> dict[str, Any]:
        """Return a full learning analysis report."""
        return self.learner.analyze_history()


__all__ = [
    "CentralRepairService",
    "DatabaseRecoveryInspector",
    "REPAIR_RECIPES",
    "RepairPlan",
    "RepairRunStore",
    "SOURCE_SELF_REPAIR_FAILURES",
    "database_integrity",
    "plan_repair",
]

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, ClassVar

from .repair_learning import (
    ErrorSignature,
    RepairLearner,
    RepairLearningStore,
    RepairOutcome,
    _normalize_error_signature,
)
from .repair_planning import (
    CENTRAL_REPAIR_VERSION,
    REPAIR_RECIPES,
    SOURCE_SELF_REPAIR_FAILURES,
    RepairPlan,
    plan_repair,
)
from .repair_inspection import (
    DatabaseRecoveryInspector,
    RepairRunStore,
    _inside,
    _iso_now,
    database_integrity,
)

class CrashDiagnoser:
    """Dynamic crash diagnosis: turn recent child stdout into an action.

    This component never writes files.  It only decides whether the crash
    is a targetable source issue, a non-source error, or an unparseable
    traceback.  The repair decision is forwarded to ``CrashRepair``; the
    actual write is deferred to the central repair dynamic program.
    """

    TAIL_LINES: ClassVar[int] = 50
    _RE_ERROR: ClassVar[re.Pattern[str]] = re.compile(
        r"^([A-Z]\w*(?:Error|Warning|Exception))\s*[:({]"
    )
    _RE_FRAME: ClassVar[re.Pattern[str]] = re.compile(
        r'^File\s+"([^"]+)",\s+line\s+(\d+),\s+in\s'
    )
    _INDENTATION_ERRORS: ClassVar[frozenset[str]] = frozenset(
        {"IndentationError", "TabError"}
    )
    # SyntaxError is only targetable when the message indicates an
    # indentation-family problem; a bare "invalid syntax" (e.g. missing
    # colon) is not fixable by the IndentationRepairer.
    _SYNTAX_INDENTATION_SIGNATURES: ClassVar[frozenset[str]] = frozenset(
        {
            "unexpected indent",
            "unindent does not match any outer indentation level",
            "expected an indented block",
            "inconsistent use of tabs and spaces",
        }
    )

    def diagnose(
        self,
        child_output: list[str],
        project_root: Path,
    ) -> dict[str, object]:
        """Parse the child output and return a diagnosis dict."""
        diagnosis: dict[str, object] = {
            "action": "fallback",
            "error_type": "",
            "file": "",
            "line": None,
        }
        if not child_output:
            return diagnosis

        error_type = ""
        error_file = ""
        error_line: int | None = None
        for raw in reversed(child_output[-self.TAIL_LINES :]):
            stripped = raw.strip()
            if not error_type:
                match = self._RE_ERROR.match(stripped)
                if match:
                    error_type = match.group(1)
            if not error_file:
                match = self._RE_FRAME.match(stripped)
                if match:
                    raw_file = match.group(1)
                    try:
                        error_line = int(match.group(2))
                        resolved = Path(raw_file).resolve()
                        error_file = resolved.relative_to(
                            project_root.resolve()
                        ).as_posix()
                    except (OSError, ValueError):
                        error_file = raw_file

        diagnosis["error_type"] = error_type
        diagnosis["file"] = error_file
        diagnosis["line"] = error_line

        if not error_file or not error_type:
            diagnosis["action"] = "fallback"
        elif error_type in self._INDENTATION_ERRORS:
            diagnosis["action"] = "targeted"
        elif error_type == "SyntaxError":
            # Only targetable if the message indicates an indentation
            # family issue; bare "invalid syntax" is not fixable by
            # the IndentationRepairer.
            tail = " ".join(
                line.strip()
                for line in child_output[-self.TAIL_LINES :]
            ).casefold()
            if any(sig in tail for sig in self._SYNTAX_INDENTATION_SIGNATURES):
                diagnosis["action"] = "targeted"
            else:
                diagnosis["action"] = "skip"
        else:
            diagnosis["action"] = "skip"
        return diagnosis


class CrashRepair:
    """Crash repair planner for the boot core.

    The boot core must not reset/overwrite source code on every startup
    failure.  This component only writes a repair signal to the
    information layer via ``RepairCoordinator``.  Actual execution is
    deferred to the maintenance sovereign's repair decision chain.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()

    def repair(self, diagnosis: dict[str, object]) -> dict[str, Any]:
        """Write a repair signal to the information layer (A67/A72).

        Per A72: ``BOOT-CORE:signal-and-request-only``.  The boot core
        must NOT execute repair mutations.  It only writes a signal to
        ``repair-requests.json`` via ``RepairCoordinator`` so the
        maintenance sovereign can pick it up, make the repair decision,
        and dispatch the governed executor.
        """
        report: dict[str, Any] = {
            "operation": "crash-repair-signal",
            "authority": "boot-core",
            "ok": False,
            "reason": "",
        }

        action = str(diagnosis.get("action", "fallback"))
        if action == "skip":
            report["reason"] = f"non-source: {diagnosis.get('error_type', 'unknown')}"
            return report
        if action != "targeted":
            report["reason"] = "unparseable-traceback; no targeted repair"
            return report

        # A72: write signal to the information layer via RepairCoordinator.
        # The boot core is a separate process from the backend, so it
        # instantiates its own RepairCoordinator pointing at the same
        # shared state file.
        try:
            from .repair_coordinator import RepairCoordinator

            coordinator = RepairCoordinator(self.project_root)
            target_file = str(diagnosis.get("file", ""))
            decision_proof = {
                "source": "boot-core-crash-diagnosis",
                "diagnosis": diagnosis,
                "exit_context": {
                    "error_type": str(diagnosis.get("error_type", "")),
                    "file": target_file,
                    "line": diagnosis.get("line"),
                },
            }
            signal_report = coordinator.request_governed_repair(
                failure_code="MAIN_SYSTEM_CRASH_REPAIR",
                owner="boot-core-crash-repair",
                decision_proof=decision_proof,
                signal_only=True,
            )
            report["ok"] = bool(signal_report.get("ok"))
            report["signal"] = signal_report
            report["targeted_file"] = target_file
            report["repair_plan"] = {
                "action": "targeted",
                "target_file": target_file,
            }
            report["reason"] = signal_report.get(
                "reason", "signal-written-to-information-layer"
            )
        except Exception as error:
            report["ok"] = False
            report["error"] = f"{type(error).__name__}: {error}"
        return report


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

    def self_repair_targeted_source(self, relative_path: str) -> dict[str, Any]:
        """Repair a single source file identified by crash diagnosis.

        Unlike ``self_repair_main_system_sources`` which scans every Python
        file, this only touches the specific file that the traceback pointed
        to.  If the file is outside the governed source roots, has
        uncommitted git changes, or is not an indentation-family error, it
        is skipped.
        """
        from .source_repair import (
            SourceRepairService,
            syntax_problems,
            IndentationRepairer,
            _git_has_uncommitted_change,
        )

        report: dict[str, Any] = {
            "operation": "targeted-source-repair",
            "authority": "main-system",
            "target": relative_path,
            "ok": False,
            "skipped": False,
            "reason": "",
        }
        service = SourceRepairService(self.project_root)
        target = (self.project_root / relative_path).resolve()
        # Security: the file must be inside the project root.
        from .source_repair import _inside, SOURCE_ROOTS

        if not _inside(target, self.project_root):
            report["reason"] = "outside project root"
            report["skipped"] = True
            return report
        # Security: the file must be inside a governed source root.
        target_relative = target.relative_to(self.project_root).as_posix()
        if not any(
            target_relative.startswith(str(root).rstrip("/") + "/")
            for root in SOURCE_ROOTS
        ):
            report["reason"] = "outside governed source roots"
            report["skipped"] = True
            return report
        if not target.is_file():
            report["reason"] = "file not found"
            report["skipped"] = True
            return report
        # Only repair Python files.
        if target.suffix != ".py":
            report["reason"] = "not a Python file"
            report["skipped"] = True
            return report
        # Check if the file actually has a syntax problem.
        problem = syntax_problems(target)
        if problem.get("ok"):
            report["reason"] = "file compiles; not a source issue"
            report["skipped"] = True
            return report
        if not problem.get("indentation_family"):
            report["reason"] = f"not indentation-family: {problem.get('error')}"
            report["skipped"] = True
            return report
        # Skip files with uncommitted developer changes.
        if _git_has_uncommitted_change(self.project_root, target):
            report["reason"] = "uncommitted git changes"
            report["skipped"] = True
            return report
        # Skip hot-reload protected files.
        if service._hot_reload_protected(target):
            report["reason"] = "hot-reload protected"
            report["skipped"] = True
            return report
        # Attempt the repair.
        try:
            repairer = IndentationRepairer(target.read_text(encoding="utf-8"))
            repaired_source, repaired_indices = repairer.repair()
        except (OSError, UnicodeError, ValueError) as error:
            report["reason"] = f"repair failed: {error}"
            self._learn_from_problem(
                {"file": relative_path, **problem}, report
            )
            return report
        try:
            service._backup(target)
            service._atomic_write(target, repaired_source)
        except (OSError, PermissionError) as error:
            report["reason"] = f"write failed: {error.__class__.__name__}"
            return report
        # Verify the repair.
        verification = syntax_problems(target)
        if not verification.get("ok"):
            report["reason"] = "post-verification failed"
            try:
                service._restore_latest(target)
            except (OSError, PermissionError):
                pass
            return report
        report["ok"] = True
        report["repaired_lines"] = repaired_indices
        report["verification"] = "compile-ok"
        # Learn from this targeted repair.
        self._learn_from_repair(
            {"file": relative_path, "repaired_lines": repaired_indices},
            report,
        )
        return report

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
    "CrashDiagnoser",
    "CrashRepair",
    "DatabaseRecoveryInspector",
    "REPAIR_RECIPES",
    "RepairPlan",
    "RepairRunStore",
    "SOURCE_SELF_REPAIR_FAILURES",
    "database_integrity",
    "plan_repair",
]

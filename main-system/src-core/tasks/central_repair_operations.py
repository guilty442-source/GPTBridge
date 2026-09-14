"""Repair operations mixin for CentralRepairService (A185 split).

Contains the targeted source repair, tool repair, and remedy
suggestion methods extracted from CentralRepairService.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .repair_learning import (
    ErrorSignature,
    RepairOutcome,
    _normalize_error_signature,
)
from .repair_planning import (
    plan_repair,
)
from .repair_inspection import (
    DatabaseRecoveryInspector,
    RepairRunStore,
    _inside,
    _iso_now,
)


class CentralRepairOperationsMixin:
    """Repair and remedy operations for CentralRepairService."""

    project_root: Path
    store: RepairRunStore
    learner: Any
    VERSION: str

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
        from .source_repair import SOURCE_ROOTS

        if not _inside(target, self.project_root):
            report["reason"] = "outside project root"
            report["skipped"] = True
            return report
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
        if target.suffix != ".py":
            report["reason"] = "not a Python file"
            report["skipped"] = True
            return report
        problem = syntax_problems(target)
        if problem.get("ok"):
            report["reason"] = "file compiles; not a source issue"
            report["skipped"] = True
            return report
        if not problem.get("indentation_family"):
            report["reason"] = f"not indentation-family: {problem.get('error')}"
            report["skipped"] = True
            return report
        if _git_has_uncommitted_change(self.project_root, target):
            report["reason"] = "uncommitted git changes"
            report["skipped"] = True
            return report
        if service._hot_reload_protected(target):
            report["reason"] = "hot-reload protected"
            report["skipped"] = True
            return report
        try:
            repairer = IndentationRepairer(target.read_text(encoding="utf-8"))
            repaired_source, repaired_indices = repairer.repair()
        except (OSError, UnicodeError, ValueError) as error:
            report["reason"] = f"repair failed: {error}"
            self._learn_from_problem(
                {"file": relative_path, **problem}, report
            )
            return report
        if _git_has_uncommitted_change(self.project_root, target):
            report["reason"] = "uncommitted git changes detected during repair"
            report["skipped"] = True
            return report
        try:
            service._backup(target)
            service._atomic_write(target, repaired_source)
        except (OSError, PermissionError) as error:
            report["reason"] = f"write failed: {error.__class__.__name__}"
            return report
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
        self._learn_from_repair(
            {"file": relative_path, "repaired_lines": repaired_indices},
            report,
        )
        return report

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
        plan = plan_repair(failure_code, recipes=self.known_recipes())
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
        self._learn_from_tool_repair(target_id, plan.failure_code, result)
        return result

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
        record_error: bool = True,
    ) -> None:
        """Record a connection/sync failure outcome with a consistent signature.

        The signature is derived from (failure_code, from_state->to_state,
        "ipc/connection") so that record and lookup use the same components.
        This closes the learning loop for connection failures: the same
        signature used to record an outcome is used to look up suggestions.

        ``record_error=False`` records only the outcome (used for recovery
        events that close an already-registered fault signature, so normal
        reconnections are not re-counted as new errors).
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
            if record_error:
                self.learner.learn_from_outcome(sig, outcome)
            else:
                self.learner.store.record_outcome(outcome)
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


__all__ = ["CentralRepairOperationsMixin"]

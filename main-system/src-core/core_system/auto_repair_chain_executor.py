"""Governed executor — applies minimal targeted patches only.

FORBID: overwrite, reset, replacement, restore-default, checkout, clean, revert, delete-recreate,
        whole-file-rewrite, directory-sync, version-selection, uncommitted-loss, self-authorization, self-verification
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core_system.auto_repair_chain_types import (
    GovernanceAudit,
    PermissionGrant,
    RepairExecution,
    RepairPlan,
)
from core_system.auto_repair_chain_util import (
    atomic_write_text,
    dirty_git_paths,
    file_compiles,
)


class GovernedExecutor:
    """Governed executor: applies minimal targeted patches only.

    FORBID: overwrite, reset, replacement, restore-default, checkout, clean, revert, delete-recreate,
            whole-file-rewrite, directory-sync, version-selection, uncommitted-loss, self-authorization, self-verification
    """

    def __init__(self, project_root: Path, audit: GovernanceAudit):
        self.project_root = project_root
        self.audit = audit
        self._lock = threading.RLock()

    def execute(
        self,
        plan: RepairPlan,
        grant: PermissionGrant,
    ) -> RepairExecution:
        """Execute minimal targeted repair plan.

        Returns execution record with postimage hashes and diff.
        """
        execution = RepairExecution(
            execution_id=f"exec_{uuid.uuid4().hex[:12]}",
            plan_id=plan.plan_id,
            executor_id="governed-executor",
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            execution = self._run_plan_steps(execution, plan, grant)
        except Exception as e:
            execution = RepairExecution(
                execution_id=execution.execution_id,
                plan_id=execution.plan_id,
                executor_id=execution.executor_id,
                started_at=execution.started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
                steps_completed=execution.steps_completed,
                postimage_hashes={},
                rollback_triggered=True,
                error=str(e),
            )

        self.audit.record("repair_executed", {
            "execution_id": execution.execution_id,
            "plan_id": execution.plan_id,
            "verification": execution.verification.value if execution.verification else None,
            "rollback": execution.rollback_triggered,
            "error": execution.error,
        })

        return execution

    def _run_plan_steps(
        self,
        execution: RepairExecution,
        plan: RepairPlan,
        grant: PermissionGrant,
    ) -> RepairExecution:
        # Verify grant matches plan
        if not self._verify_grant(plan, grant):
            raise PermissionError("Grant does not cover repair plan")

        # Check for uncommitted changes (A259: stop on overlap)
        if self._has_uncommitted_changes(plan):
            raise PermissionError("Uncommitted changes overlap repair target")

        # Execute each step
        completed_steps = []
        for step in plan.steps:
            step_result = self._execute_step(step, grant)
            completed_steps.append(step_result)

            if not step_result.get("ok", False):
                # Trigger rollback
                self._rollback(plan, execution)
                return RepairExecution(
                    execution_id=execution.execution_id,
                    plan_id=execution.plan_id,
                    executor_id=execution.executor_id,
                    started_at=execution.started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    steps_completed=completed_steps,
                    postimage_hashes={},
                    rollback_triggered=True,
                    error=f"Step failed: {step_result.get('error')}",
                )

        # All steps succeeded - compute postimage hashes
        post_hashes = self._compute_hashes(plan.preimage_hashes.keys())
        diff = self._generate_diff(plan.preimage_hashes, post_hashes)

        return RepairExecution(
            execution_id=execution.execution_id,
            plan_id=execution.plan_id,
            executor_id=execution.executor_id,
            started_at=execution.started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            steps_completed=completed_steps,
            postimage_hashes=post_hashes,
            diff=diff,
        )

    def _verify_grant(self, plan: RepairPlan, grant: PermissionGrant) -> bool:
        """Verify grant covers all plan targets (boundary-aware match)."""
        scopes = [scope.rstrip("/") for scope in grant.path_scope]
        for path in plan.preimage_hashes.keys():
            if not any(path == scope or path.startswith(scope + "/") for scope in scopes):
                return False
        return True

    def _has_uncommitted_changes(self, plan: RepairPlan) -> bool:
        """Check for uncommitted git changes in target files.

        One ``git status`` call for the whole plan instead of one
        subprocess per file.  An unverifiable working tree counts as
        dirty (fail-closed, same contract as ``tasks.source_repair``).
        """
        dirty = dirty_git_paths(self.project_root)
        if dirty is None:
            return True
        return any(path in dirty for path in plan.preimage_hashes.keys())

    def _execute_step(self, step: dict[str, Any], grant: PermissionGrant) -> dict[str, Any]:
        """Execute a single repair step."""
        action = step.get("action")
        target = step.get("target")

        if action == "targeted_patch":
            return self._apply_targeted_patch(target, step.get("patch"))
        elif action == "rebuild_artifact":
            return self._rebuild_artifact(target, grant)
        elif action == "config_reset":
            return self._reset_config(target, step.get("config"))
        else:
            return {"ok": False, "error": f"Unknown action: {action}"}

    def _apply_targeted_patch(self, target: str, patch: Optional[str]) -> dict[str, Any]:
        """Apply the bounded deterministic repair for indentation-family faults.

        The patch is produced in-process by ``IndentationRepairer`` — the
        same bounded, unique-solution-only engine the governed
        ``tasks.source_repair`` path uses.  An explicit ``patch`` payload
        is rejected: replacing a file with caller-supplied content is the
        FORBID whole-file-rewrite path.
        """
        if patch:
            return {
                "ok": False,
                "error": "caller-supplied patch content is forbidden (whole-file-rewrite)",
            }

        full_path = self.project_root / target
        if not full_path.is_file():
            return {"ok": False, "error": f"Target not found: {target}"}

        try:
            source = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            return {"ok": False, "error": f"unreadable: {error.__class__.__name__}"}

        from tasks.source_repair_indent import IndentationRepairer

        try:
            repaired_source, repaired_indices = IndentationRepairer(source).repair()
        except ValueError as error:
            if "already compiles" in str(error):
                return {"ok": True, "action": "targeted_patch", "target": target, "changed": False}
            return {"ok": False, "error": f"deterministic repair unavailable: {error}"}
        except (OSError, UnicodeError) as error:
            return {"ok": False, "error": str(error)}

        backup_path = full_path.with_suffix(full_path.suffix + ".repair_backup")
        try:
            shutil.copy2(full_path, backup_path)
            atomic_write_text(full_path, repaired_source)
        except (OSError, PermissionError) as error:
            return {"ok": False, "error": f"write failed: {error.__class__.__name__}"}

        ok, compile_error = file_compiles(full_path)
        if not ok:
            shutil.copy2(backup_path, full_path)
            backup_path.unlink(missing_ok=True)
            return {"ok": False, "error": f"post-patch verification failed: {compile_error}"}

        # Backup is retained on purpose: it is the rollback material held
        # until finalize() observes the independent verification verdict.
        return {
            "ok": True,
            "action": "targeted_patch",
            "target": target,
            "changed": True,
            "repaired_indices": repaired_indices,
        }

    def _rebuild_artifact(self, target: str, grant: PermissionGrant) -> dict[str, Any]:
        """Rebuild tool artifact via the governed platform packager."""
        del grant  # scope was already verified against the plan
        try:
            from tasks.package_rebuilder import ToolPackageRebuilder

            rebuilder = ToolPackageRebuilder(
                self.project_root,
                self.project_root / "main-system",
            )
            result = rebuilder.rebuild(target)
        except Exception as error:  # fail-closed: no partial rebuild
            return {"ok": False, "error": f"rebuilder unavailable: {error.__class__.__name__}"}
        return {
            "ok": bool(result.get("ok")),
            "action": "rebuild_artifact",
            "target": target,
            "error": result.get("error") or result.get("error_code"),
        }

    def _reset_config(self, target: str, config: Optional[dict]) -> dict[str, Any]:
        """Config reset is not wired to a certified baseline — fail closed."""
        return {
            "ok": False,
            "error": "config_reset has no certified-baseline source; refusing no-op success",
            "action": "config_reset",
            "target": target,
        }

    def _compute_hashes(self, paths: list[str]) -> dict[str, str]:
        """Compute SHA256 hashes of files."""
        hashes = {}
        for path in paths:
            full_path = self.project_root / path
            if full_path.exists():
                hashes[path] = hashlib.sha256(full_path.read_bytes()).hexdigest()
        return hashes

    def _generate_diff(self, pre: dict[str, str], post: dict[str, str]) -> str:
        """Generate diff between pre and post states for audit trail only."""
        diff_lines = []
        for path in set(pre.keys()) | set(post.keys()):
            if pre.get(path) != post.get(path):
                diff_lines.append(f"M {path}: {pre.get(path, 'new')} -> {post.get(path, 'deleted')}")
        return "\n".join(diff_lines) if diff_lines else "no changes"

    def finalize(self, plan: RepairPlan, verification_passed: bool) -> dict[str, Any]:
        """Resolve retained backups after independent verification.

        ``verification_passed=True`` discards rollback material; ``False``
        restores every patched file from its ``.repair_backup`` preimage
        so a repair that failed verification leaves no residue.
        """
        restored: list[str] = []
        discarded: list[str] = []
        for path in plan.preimage_hashes.keys():
            full_path = self.project_root / path
            backup_path = full_path.with_suffix(full_path.suffix + ".repair_backup")
            if not backup_path.exists():
                continue
            if verification_passed:
                backup_path.unlink(missing_ok=True)
                discarded.append(path)
            else:
                shutil.copy2(backup_path, full_path)
                backup_path.unlink(missing_ok=True)
                restored.append(path)
        return {"restored": restored, "discarded": discarded}

    def _rollback(self, plan: RepairPlan, execution: RepairExecution) -> None:
        """Rollback using backup files (NOT snapshot hashes)."""
        for path in plan.preimage_hashes.keys():
            full_path = self.project_root / path
            backup_path = full_path.with_suffix(full_path.suffix + ".repair_backup")
            if backup_path.exists():
                shutil.copy2(backup_path, full_path)
                backup_path.unlink(missing_ok=True)


__all__ = ["GovernedExecutor"]

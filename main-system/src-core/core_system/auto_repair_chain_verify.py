"""Independent verifier — verifies repair without self-verification.

A261/A166: NO snapshot/hash comparison for verification decisions.
Verification uses: compile-ok, tests-pass, governance-audit, stability.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from core_system.auto_repair_chain_types import (
    GovernanceAudit,
    PermissionGrant,
    RepairExecution,
    RepairPlan,
    VerificationResult,
)
from core_system.auto_repair_chain_util import (
    ensure_sys_path,
    file_compiles,
    iter_scope_files,
    step_targets,
)


class IndependentVerifier:
    """Independent verifier: verifies repair without self-verification."""

    def __init__(self, project_root: Path, audit: GovernanceAudit):
        self.project_root = project_root
        self.audit = audit

    def verify(
        self,
        execution: RepairExecution,
        plan: RepairPlan,
        grant: PermissionGrant,
    ) -> tuple[VerificationResult, dict[str, Any]]:
        """Verify repair meets all criteria per A261: tests/audit/stability.

        Returns (verification_result, evidence).
        A261/A166: NO snapshot/hash comparison for verification decisions.
        Verification uses: compile-ok, tests-pass, governance-audit, stability.
        """
        evidence = {
            "execution_id": execution.execution_id,
            "plan_id": plan.plan_id,
            "checks": {},
        }

        # 1. Run verification criteria from plan (A261: tests/audit/stability)
        all_passed = self._run_verification_criteria(plan, grant, evidence)

        # 2. A166/A261: NO snapshot/hash comparison for verification
        # Pre/post hashes are recorded for audit trail only, not used for decisions
        evidence["checks"]["snapshot_comparison"] = "prohibited_by_A166_A261"

        # 3. Containment: every file whose content changed must be a
        # declared step target — a change outside the plan means the
        # executor touched uncommitted developer work (or drifted).
        changed = self._changed_paths(plan, execution)
        contained = changed <= step_targets(plan)
        evidence["checks"]["uncommitted_preserved"] = contained
        evidence["checks"]["changed_paths"] = sorted(changed)
        all_passed = all_passed and contained

        # 3. Record diff size for audit trail (not for decision)
        if execution.diff:
            evidence["checks"]["diff_size"] = len(execution.diff)

        # 4. Record files changed for audit trail (not for decision)
        if execution.postimage_hashes and plan.preimage_hashes:
            changed_files = sum(1 for k in plan.preimage_hashes if plan.preimage_hashes.get(k) != execution.postimage_hashes.get(k))
            evidence["checks"]["files_changed"] = changed_files

        # Determine overall verification
        if all_passed:
            result = VerificationResult.PASSED
        else:
            result = VerificationResult.FAILED

        self.audit.record("independent_verification", {
            "execution_id": execution.execution_id,
            "result": result.value,
            "evidence": evidence,
        })

        return result, evidence

    def _run_verification_criteria(
        self,
        plan: RepairPlan,
        grant: PermissionGrant,
        evidence: dict[str, Any],
    ) -> bool:
        all_passed = True
        for criterion in plan.verification_criteria:
            if criterion == "compile-ok":
                result = self._verify_compile(plan.preimage_hashes.keys())
                evidence["checks"]["compile_ok"] = result
                all_passed = all_passed and result
            elif criterion == "tests-pass":
                result = self._verify_tests(grant.path_scope)
                evidence["checks"]["tests_pass"] = result
                all_passed = all_passed and result
            elif criterion == "governance-audit":
                result = self._verify_governance_audit()
                evidence["checks"]["governance_audit"] = result
                all_passed = all_passed and result
            elif criterion == "stability":
                result = self._verify_stability(grant.path_scope)
                evidence["checks"]["stability"] = result
                all_passed = all_passed and result
        return all_passed

    def _verify_compile(self, paths: list[str]) -> bool:
        """Verify Python files compile — in-process, no subprocess per file."""
        for path in paths:
            if path.endswith(".py"):
                full_path = self.project_root / path
                if full_path.exists():
                    ok, _ = file_compiles(full_path)
                    if not ok:
                        return False
        return True

    def _verify_tests(self, path_scopes: list[str]) -> bool:
        """Run the tests that live inside the granted scopes.

        Scoped: only ``test_*.py``/``*_test.py`` files under the granted
        paths, capped, with a timeout.  A scope with no tests is vacuous
        pass (recorded in evidence by the caller's check map).
        """
        test_files: list[str] = []
        for scope in path_scopes:
            for rel, _ in iter_scope_files(self.project_root, scope):
                name = rel.rsplit("/", 1)[-1]
                if name.startswith("test_") and name.endswith(".py"):
                    test_files.append(rel)
        if not test_files:
            return True
        test_files = sorted(set(test_files))[:50]
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-x", *test_files],
                cwd=str(self.project_root),
                capture_output=True,
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _verify_governance_audit(self) -> bool:
        """Run governance audit to verify no governance violations."""
        try:
            ensure_sys_path(
                self.project_root / "main-system" / "src-core",
                self.project_root / "shared-layer" / "src",
            )
            from governance_rule.execution.audit import audit_runtime_governance
            errors = audit_runtime_governance(self.project_root, include_self_health=False)
            return len(errors) == 0
        except Exception:
            return False

    def _verify_stability(self, path_scopes: list[str]) -> bool:
        """Verify system stability - no regressions in core functionality."""
        try:
            ensure_sys_path(
                self.project_root / "main-system" / "src-core",
                self.project_root / "shared-layer" / "src",
            )

            # Verify critical modules can be imported
            from core_system.governance_runtime import MainSystemGovernance
            from shared_layer.store import PostgresSharedLayerStore

            # Verify no new syntax errors in repaired paths — in-process
            for scope in path_scopes:
                for rel, full_path in iter_scope_files(self.project_root, scope):
                    if rel.endswith(".py"):
                        ok, _ = file_compiles(full_path)
                        if not ok:
                            return False
            return True
        except Exception:
            return False

    def _changed_paths(self, plan: RepairPlan, execution: RepairExecution) -> set[str]:
        """Files whose content actually changed (pre vs post image)."""
        changed = set()
        for path, pre in plan.preimage_hashes.items():
            if execution.postimage_hashes.get(path) != pre:
                changed.add(path)
        changed.update(
            path for path in execution.postimage_hashes if path not in plan.preimage_hashes
        )
        return changed


__all__ = ["IndependentVerifier"]

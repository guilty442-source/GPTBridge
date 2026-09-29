"""Git Disaster Recovery Manager (Git layer only).

Evidence, diagnosis, backup, recovery planning and verification — nothing
else.  This module never touches SQL, RAG, LLM runtime or application data,
and it NEVER executes high-risk operations:

    no reset --hard, no force push, no history rewrite, no ref deletion,
    no reflog expiry, no aggressive prune.

Recovery refs (``refs/gptbridge/recovery/<id>``) are anchors, not release
authority.  Tier-3 actions appear only as *proposals* inside a recovery plan
and require governance authority approval.

Module layout (A185 source-size split):

    disaster_recovery_types.py     constants + record dataclasses
    disaster_recovery_capture.py   DRCaptureMixin — plumbing/points/bundles
    disaster_recovery_diagnose.py  DRDiagnoseMixin — fsck/matrix/integrity
    disaster_recovery_plan.py      DRPlanMixin — emergency/plan/metrics
    disaster_recovery.py           GitDisasterRecovery seam (this module)
"""

from __future__ import annotations

import os
from pathlib import Path

from . import audit_chain, audit_log
from .disaster_recovery_capture import DRCaptureMixin
from .disaster_recovery_diagnose import DRDiagnoseMixin
from .disaster_recovery_plan import DRPlanMixin
from .disaster_recovery_types import (  # noqa: F401  (re-exported surface)
    AUTOMATION_STATE_RELATIVE,
    BUNDLE_KINDS,
    DEFAULT_BACKUP_ROOT,
    EMERGENCY_MODES,
    READ_ONLY_ALLOWED,
    RECOVERY_REF_PREFIX,
    WORKTREE_STATES,
    BundleManifest,
    DisasterRecoveryError,
    RecoveryPlan,
    RecoveryPlanOption,
    RecoveryPoint,
    RepositoryDiagnosis,
    WorktreeIntegrity,
    _FSCK_PATTERNS,
    _INTEGRITY_STATES,
    _sha256_file,
    _sha256_text,
)
from .paths import contained


class GitDisasterRecovery(DRCaptureMixin, DRDiagnoseMixin, DRPlanMixin):
    """Bounded Git-only recovery manager."""

    def __init__(
        self,
        root: str | Path = r"E:\GPTBridge",
        *,
        backup_root: str | Path | None = None,
        enable_audit: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        configured = backup_root or os.environ.get("GPTBRIDGE_GIT_BACKUP_ROOT")
        self.backup_root = contained(
            self.root,
            configured or DEFAULT_BACKUP_ROOT,
            purpose="backup-root",
        )
        self.enable_audit = enable_audit

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

    def _audit(self, tier: int, operation: str, detail: str) -> None:
        if not self.enable_audit:
            return
        try:
            entry = audit_log(
                tier,
                f"dr:{operation}",
                "governance/git-disaster-recovery",
                True,
                detail,
                operation="dr:" + operation,
                phase="result",
                result="succeeded",
            )
            audit_chain.append_audit(dict(entry))
        except Exception:
            pass


__all__ = [
    "AUTOMATION_STATE_RELATIVE",
    "BUNDLE_KINDS",
    "DEFAULT_BACKUP_ROOT",
    "DisasterRecoveryError",
    "EMERGENCY_MODES",
    "GitDisasterRecovery",
    "READ_ONLY_ALLOWED",
    "RECOVERY_REF_PREFIX",
    "BundleManifest",
    "RecoveryPlan",
    "RecoveryPlanOption",
    "RecoveryPoint",
    "RepositoryDiagnosis",
    "WorktreeIntegrity",
]

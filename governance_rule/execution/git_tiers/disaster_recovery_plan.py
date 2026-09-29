"""Git disaster-recovery plan/emergency mixin (A185 split).

Extracted from ``disaster_recovery.py`` (source-size contract): emergency
mode state (§151-153 — ``evaluate_emergency_mode`` only *proposes*;
``auto_apply`` is opt-in), the §126/§155 recovery plan (Tier-3 actions
appear only as *proposals* requiring governance authority approval),
post-abort verification and the §156 metrics summary.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .disaster_recovery_types import (
    AUTOMATION_STATE_RELATIVE,
    DisasterRecoveryError,
    EMERGENCY_MODES,
    RecoveryPlan,
    RecoveryPlanOption,
)


class DRPlanMixin:
    """Emergency-mode + recovery-plan surface."""

    # ------------------------------------------------------------------
    # emergency modes (151-153)
    # ------------------------------------------------------------------

    def emergency_mode(self) -> str:
        path = self._common_git_dir() / AUTOMATION_STATE_RELATIVE / "emergency-mode.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            mode = str(payload.get("mode") or "NORMAL")
            return mode if mode in EMERGENCY_MODES else "UNKNOWN"
        except (OSError, ValueError):
            return "NORMAL"

    def set_emergency_mode(self, mode: str, reason: str, *, actor: str = "git-disaster-recovery") -> None:
        if mode not in EMERGENCY_MODES:
            raise DisasterRecoveryError(f"EMERGENCY_MODE_INVALID:{mode}")
        path = self._common_git_dir() / AUTOMATION_STATE_RELATIVE / "emergency-mode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": mode,
            "reason": reason,
            "actor": actor,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        self._audit(2, "emergency-mode", f"mode={mode} reason={reason}")

    def read_only_triggers(self) -> list[str]:
        triggers: list[str] = []
        diagnosis = self.diagnose_repository(deep=True)
        if diagnosis.state == "CORRUPT":
            triggers.append("object-corruption")
        if self.audit_ledger_health()["state"] == "CORRUPT":
            triggers.append("audit-corruption")
        if self.registry_health()["state"] == "CORRUPT":
            triggers.append("registry-corruption")
        if self.hook_integrity()["state"] == "FAILED":
            triggers.append("hook-integrity-failure")
        matrix = self.revision_matrix()
        if matrix["classification"] in ("MULTI_DIVERGED", "LOCAL_DIVERGED"):
            triggers.append("remote-divergence-unknown")
        return triggers

    def evaluate_emergency_mode(self, *, auto_apply: bool = False) -> dict[str, Any]:
        triggers = self.read_only_triggers()
        proposal = "READ_ONLY" if triggers else "NORMAL"
        if auto_apply and proposal == "READ_ONLY":
            self.set_emergency_mode("READ_ONLY", ",".join(triggers))
        return {"current": self.emergency_mode(), "proposed": proposal, "triggers": triggers}

    # ------------------------------------------------------------------
    # recovery plan (126, 155) + verification (127, 132-133)
    # ------------------------------------------------------------------

    def prepare_recovery_plan(self, incident_id: str, severity: str = "high") -> RecoveryPlan:
        main = self.ref_value("refs/heads/main")
        matrix = self.revision_matrix()
        points = self.list_recovery_points()
        bundles = self.list_bundles()
        worktrees = self.worktree_integrity()
        diagnosis = self.diagnose_repository(deep=True)
        interrupted = self.interrupted_state()
        plan = RecoveryPlan(
            incident_id=incident_id,
            detected_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            severity=severity,
            repository_state=diagnosis.state,
            known_good_revision=points[-1].main_revision if points else "",
            current_revision=main,
            origin_revision=matrix["origin"],
            recovery_points=[point.recovery_id for point in points][-10:],
            bundle_available=[str(entry.get("name") or "") for entry in bundles][-5:],
            affected_worktrees=[item.path for item in worktrees if item.state != "HEALTHY"],
            affected_branches=[],
            audit_status=self.audit_ledger_health()["state"],
            classification=matrix["classification"],
        )
        anchor = plan.known_good_revision or main
        plan.options = [
            RecoveryPlanOption(
                option_id="OPTION_A",
                summary="現地修正：以新 commit 修復合併內容（不改寫歷史）",
                git_commands=[
                    "# (governed) restore/repair working tree, then:",
                    "git add <repaired paths>",
                    "git commit -m 'repair: <incident>'",
                ],
                risk_tier=2,
                expected_result="main 前進一個修復 commit；不重寫既有歷史",
                rollback_anchor=anchor,
                required_approval="coordinator + governance audit PASS",
            ),
            RecoveryPlanOption(
                option_id="OPTION_B",
                summary="受治理 revert 合併 commit（保留原始歷史）",
                git_commands=[
                    f"git revert -m 1 <merge-sha>  # 需治理批准;HEAD={main[:12]}",
                ],
                risk_tier=3,
                expected_result="產生反向 commit；歷史保留",
                rollback_anchor=anchor,
                required_approval="governance authority explicit approval",
            ),
            RecoveryPlanOption(
                option_id="OPTION_C",
                summary="從 bundle/中央/遠端證據重建（僅在 object 損壞時）",
                git_commands=[
                    "git bundle verify <bundle>",
                    f"git clone <bundle> <temp>  # 於暫存目錄驗證;anchor={anchor[:12]}",
                ],
                risk_tier=3,
                expected_result="以最近 verified bundle 重建；需人工驗證後切換",
                rollback_anchor=anchor,
                required_approval="governance authority + release certification",
            ),
        ]
        if interrupted["state"] == "INTERRUPTED":
            plan.options.insert(
                0,
                RecoveryPlanOption(
                    option_id="OPTION_0",
                    summary="完成/中止中斷中的 Git 操作（需 transaction metadata）",
                    git_commands=[
                        "git status  # 僅允許診斷;不得自動 continue/abort",
                        "git merge --abort  # 僅 coordinator 且需完整 transaction metadata",
                    ],
                    risk_tier=2,
                    expected_result="回到 pre-merge HEAD 並驗證 index/worktree/merge metadata",
                    rollback_anchor=anchor,
                    required_approval="coordinator with recorded transaction_id",
                ),
            )
        return plan

    def verify_recovered_state(self, pre_merge_head: str) -> list[str]:
        """Post-abort verification: never trust only the exit code."""
        errors: list[str] = []
        head = self.ref_value("refs/heads/main") or self._try(["rev-parse", "HEAD"]).strip()
        if pre_merge_head and head != pre_merge_head:
            errors.append(f"HEAD_MISMATCH:expected={pre_merge_head[:12]}:actual={head[:12]}")
        state = self.interrupted_state()
        for key in ("merge_head", "cherry_pick_head", "revert_head", "rebase_merge", "rebase_apply"):
            if state[key]:
                errors.append(f"INTERRUPTED_STATE_REMAINS:{key}")
        if state["index_lock"]:
            errors.append("INDEX_LOCK_REMAINS")
        return errors

    # ------------------------------------------------------------------
    # metrics (156)
    # ------------------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        point = self.latest_recovery_point()
        bundles = self.list_bundles()
        age = None
        if point:
            try:
                created = time.mktime(time.strptime(point.timestamp[:19], "%Y-%m-%dT%H:%M:%S"))
                age = round((time.time() - created) / 3600.0, 2)
            except ValueError:
                age = None
        return {
            "last_recovery_point": point.recovery_id if point else "",
            "recovery_point_age_hours": age,
            "last_verified_bundle": next(
                (entry.get("name") for entry in reversed(bundles) if entry.get("verified")), ""
            ),
            "bundle_count": len(bundles),
            "audit_sequence": self.audit_sequence(),
            "mode": self.emergency_mode(),
        }


__all__ = ["DRPlanMixin"]

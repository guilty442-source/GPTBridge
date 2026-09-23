"""Compliance monitor — A436 execution compliance monitoring.

負責：
1. 持續監控執行合規性
2. 自動檢測違規
3. 風險評分與告警
4. 自動整改建議
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)
from governance_rule.permission_directory.code_rule_directory import code_rule_directory_snapshot

from core_system.permission_automation_types import (
    ComplianceSeverity,
    ComplianceViolation,
)

_logger = logging.getLogger("gptbridge.permission_automation")


class ComplianceMonitor:
    """合規監控器。

    負責：
    1. 持續監控執行合規性
    2. 自動檢測違規
    3. 風險評分與告警
    4. 自動整改建議
    """

    def __init__(
        self,
        permission_sovereign: Any,
        check_interval: float = 60.0,  # 1分鐘
    ) -> None:
        self.permission_sovereign = permission_sovereign
        self.check_interval = check_interval
        self._running = False
        self._task: asyncio.Task | None = None

        self._violations: dict[str, ComplianceViolation] = {}
        self._violation_counter = 0
        self._risk_scores: dict[str, float] = defaultdict(float)
        # Fingerprints of source violations already ingested — the
        # sovereign's ``_compliance_violations`` list is cumulative, so
        # without dedup every cycle would re-ingest all of them.
        self._seen_violations: set[str] = set()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="compliance-monitor")
        _logger.info("ComplianceMonitor started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _logger.info("ComplianceMonitor stopped")

    async def run_once(self) -> None:
        """單次合規檢查——供 automation core 外部驅動（§1.1 自動化集中）。"""
        await self._check_compliance()

    async def _run_loop(self) -> None:
        while self._running:
            # Fallback private loop only runs with no automation core;
            # bound the tick so a hung check cannot freeze it silently
            # (same contract the core's wait_for wrapper gives).
            tick_deadline = max(30.0, min(600.0, float(self.check_interval) * 5))
            try:
                await asyncio.wait_for(self._check_compliance(), timeout=tick_deadline)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _logger.warning(
                    "ComplianceMonitor tick exceeded %.0fs deadline", tick_deadline
                )
            except Exception as e:
                _logger.error(f"Compliance check failed: {e}")
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    async def _check_compliance(self) -> None:
        """執行合規檢查。"""
        # 1. 檢查權限主宰記錄的違規
        if hasattr(self.permission_sovereign, '_compliance_violations'):
            for violation in self.permission_sovereign._compliance_violations:
                await self._process_violation(violation)

        # 2. 檢查目錄完整性
        await self._check_directory_integrity()

        # 3. 檢查授予狀態一致性
        await self._check_grant_consistency()

        # 4. 更新風險評分
        self._update_risk_scores()

    async def _process_violation(self, violation: dict) -> None:
        """處理違規記錄（已攝入的來源記錄去重）。"""
        import json as _json

        fingerprint = _json.dumps(
            violation, sort_keys=True, default=str
        )
        if fingerprint in self._seen_violations:
            return
        self._seen_violations.add(fingerprint)

        violation_id = f"viol-{self._violation_counter}"
        self._violation_counter += 1

        # 確定嚴重程度
        severity = self._assess_severity(violation)

        cv = ComplianceViolation(
            violation_id=violation_id,
            severity=severity,
            actor=violation.get("actor", ""),
            capability=violation.get("capability", ""),
            target=violation.get("target", ""),
            description=violation.get("violation", ""),
            detected_at=datetime.now(timezone.utc),
            auto_resolvable=severity in (ComplianceSeverity.LOW, ComplianceSeverity.MEDIUM),
        )

        self._violations[violation_id] = cv

        # 更新風險評分
        actor = violation.get("actor", "unknown")
        self._risk_scores[actor] += self._severity_weight(severity)

        # 記錄日誌
        _logger.warning(f"Compliance violation detected: {violation_id} [{severity.value}] for {actor}")

        # 高風險自動觸發審計
        if severity in (ComplianceSeverity.HIGH, ComplianceSeverity.CRITICAL):
            _logger.error(f"High severity violation: {violation_id} - triggering audit")

    def _assess_severity(self, violation: dict) -> ComplianceSeverity:
        """評估違規嚴重程度。"""
        v_type = violation.get("violation", "").lower()
        if any(kw in v_type for kw in ["unauthorized", "bypass", "escalation", "injection"]):
            return ComplianceSeverity.CRITICAL
        if any(kw in v_type for kw in ["expired", "revoked", "suspended", "unauthorized_access"]):
            return ComplianceSeverity.HIGH
        if any(kw in v_type for kw in ["mismatch", "inconsistency", "drift"]):
            return ComplianceSeverity.MEDIUM
        return ComplianceSeverity.LOW

    def _severity_weight(self, severity: ComplianceSeverity) -> float:
        weights = {
            ComplianceSeverity.LOW: 1.0,
            ComplianceSeverity.MEDIUM: 5.0,
            ComplianceSeverity.HIGH: 20.0,
            ComplianceSeverity.CRITICAL: 100.0,
        }
        return weights.get(severity, 1.0)

    async def _check_directory_integrity(self) -> None:
        """檢查目錄完整性。"""
        try:
            code_rules = code_rule_directory_snapshot()
            authority = directory_authority_snapshot()
            identities = identity_group_snapshot()

            # 檢查版本一致性：程式碼規則目錄的初始版本必須與權威政策的
            # 程式碼版本政策一致（不得以寫死的字面值放寬治理檢查）。
            if (
                code_rules.initial_code_version
                != authority.code_version_policy.initial_version
            ):
                _logger.warning(
                    "Code rule directory version mismatch: %s != %s",
                    code_rules.initial_code_version,
                    authority.code_version_policy.initial_version,
                )

            # 檢查權威版本
            if authority.authority_version_policy.current_version != authority.authority_version_policy.initial_version:
                _logger.warning("Authority version drifted from initial")

            # 檢查身份群組完整性
            for ident in identities.identities:
                if not ident.actor or not ident.identity_code:
                    _logger.warning(f"Incomplete identity record: {ident}")

        except Exception as e:
            _logger.error(f"Directory integrity check failed: {e}")

    async def _check_grant_consistency(self) -> None:
        """檢查授予狀態一致性。

        權限主宰帳本（``_issued_grants``）中的授予若其 actor/能力已不在
        封印身份權限目錄內，記為 medium 違規 — 帳本與目錄漂移屬於
        執行合規監督範圍（A436）。
        """
        issued = getattr(self.permission_sovereign, "_issued_grants", None)
        if not issued:
            return
        try:
            snapshot = identity_permission_snapshot()
            known_identities = {
                getattr(rec, "actor", None) or getattr(rec, "identity_code", None)
                for rec in getattr(snapshot, "permissions", getattr(snapshot, "identities", []))
            }
        except Exception:
            return
        for grant_id, grant in issued.items():
            actor = grant.get("actor") if isinstance(grant, dict) else getattr(grant, "actor", None)
            if actor and actor not in known_identities:
                await self._process_violation({
                    "violation": "grant-actor-not-in-directory",
                    "actor": actor,
                    "capability": grant.get("capability") if isinstance(grant, dict) else getattr(grant, "capability", ""),
                    "target": grant_id,
                })

    def _update_risk_scores(self) -> None:
        """更新風險評分（時間衰減）。"""
        now = time.time()
        for actor in list(self._risk_scores.keys()):
            # 時間衰減：每小時衰減 10%
            self._risk_scores[actor] *= 0.9
            if self._risk_scores[actor] < 0.1:
                del self._risk_scores[actor]

    def get_risk_report(self) -> dict[str, Any]:
        """獲取風險報告。"""
        return {
            "high_risk_actors": [
                {"actor": actor, "score": score}
                for actor, score in sorted(self._risk_scores.items(), key=lambda x: -x[1])
                if score > 50
            ],
            "total_violations": len(self._violations),
            "unresolved_violations": sum(1 for v in self._violations.values() if not v.resolved),
        }

    def resolve_violation(self, violation_id: str, action: str) -> bool:
        """標記違規為已解決。"""
        if violation_id in self._violations:
            v = self._violations[violation_id]
            v.resolved = True
            v.resolved_at = datetime.now(timezone.utc)
            v.resolution_action = action
            return True
        return False


__all__ = ["ComplianceMonitor"]

"""Automated Permission Lifecycle Manager — 自動化權限生命週期管理。

法典依據:
- A6: permission-sovereign supervises execution compliance
- A10/A11: explicit allowlist, fail-closed
- A22: permission termination authority
- A39: actor identity verification
- E4: OWNER:permission-sovereign; ACTIONS:manage-issue-terminate-supervise

功能:
1. 權限授予生命週期自動管理（續期、過期、撤銷）
2. 目錄同步自動化
3. 合規監控自動化
4. 審計排程自動化
5. 自我修復能力
6. 身份群組自動管理
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
from collections import defaultdict

from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)
from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
    capability_boundary_snapshot,
)
from governance_rule.permission_directory.code_rule_directory import code_rule_directory_snapshot
from governance_rule.governance_policy import governance_policy_snapshot

_logger = logging.getLogger("gptbridge.permission_automation")


class PermissionGrantState(Enum):
    """權限授予狀態。"""
    ACTIVE = "active"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    REVOKED = "revoked"
    SUSPENDED = "suspended"
    PENDING_RENEWAL = "pending_renewal"


class ComplianceSeverity(Enum):
    """合規違規嚴重程度。"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class PermissionGrant:
    """權限授予記錄。"""
    grant_id: str
    actor: str
    capability: str
    action: str
    target: str
    data_scope: Optional[str]
    issued_at: datetime
    expires_at: Optional[datetime] = None
    state: PermissionGrantState = PermissionGrantState.ACTIVE
    renewed_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoked_reason: Optional[str] = None
    auto_renew: bool = True
    renewal_count: int = 0
    last_checked: Optional[datetime] = None

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    def is_expiring_soon(self, days: int = 7) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(days=days)

    def time_until_expiry(self) -> Optional[timedelta]:
        if self.expires_at is None:
            return None
        return self.expires_at - datetime.now(timezone.utc)


@dataclass
class ComplianceViolation:
    """合規違規記錄。"""
    violation_id: str
    severity: ComplianceSeverity
    actor: str
    capability: str
    target: str
    description: str
    detected_at: datetime
    resolved: bool = False
    resolved_at: Optional[datetime] = None
    resolution_action: Optional[str] = None
    auto_resolvable: bool = False


@dataclass
class AuditSchedule:
    """審計排程。"""
    audit_id: str
    audit_type: str
    schedule: str  # cron-like or interval
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    enabled: bool = True
    last_result: Optional[dict] = None


class PermissionLifecycleManager:
    """權限生命週期管理器。

    負責：
    1. 權限授予的自動續期、過期處理、撤銷
    2. 過期前預警通知
    3. 定期清理已撤銷/過期的授予
    """

    def __init__(
        self,
        permission_sovereign: Any,
        check_interval: float = 3600.0,  # 1小時
        expiry_warning_days: int = 7,
        auto_renew_enabled: bool = True,
    ) -> None:
        self.permission_sovereign = permission_sovereign
        self.check_interval = check_interval
        self.expiry_warning_days = expiry_warning_days
        self.auto_renew_enabled = auto_renew_enabled

        self._grants: dict[str, PermissionGrant] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # 續期配置
        self._default_ttl = timedelta(days=30)
        self._max_renewals = 10

    async def start(self) -> None:
        """啟動管理器。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="permission-lifecycle-manager")
        _logger.info("PermissionLifecycleManager started")

    async def stop(self) -> None:
        """停止管理器。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _logger.info("PermissionLifecycleManager stopped")

    def register_grant(
        self,
        grant_id: str,
        actor: str,
        capability: str,
        action: str,
        target: str,
        data_scope: Optional[str] = None,
        ttl: Optional[timedelta] = None,
        auto_renew: bool = True,
    ) -> PermissionGrant:
        """註冊新權限授予。"""
        now = datetime.now(timezone.utc)
        grant = PermissionGrant(
            grant_id=grant_id,
            actor=actor,
            capability=capability,
            action=action,
            target=target,
            data_scope=data_scope,
            issued_at=now,
            expires_at=now + (ttl or self._default_ttl),
            auto_renew=auto_renew,
        )
        self._grants[grant_id] = grant
        _logger.info(f"Registered permission grant: {grant_id} for {actor}/{capability}")
        return grant

    def get_grant(self, grant_id: str) -> Optional[PermissionGrant]:
        return self._grants.get(grant_id)

    def revoke_grant(self, grant_id: str, reason: str = "") -> bool:
        """撤銷權限授予。"""
        grant = self._grants.get(grant_id)
        if grant is None:
            return False
        grant.state = PermissionGrantState.REVOKED
        grant.revoked_at = datetime.now(timezone.utc)
        grant.revoked_reason = reason
        _logger.warning(f"Revoked permission grant: {grant_id}, reason: {reason}")
        return True

    async def _run_loop(self) -> None:
        """主循環。"""
        while self._running:
            try:
                await self._check_grants()
            except Exception as e:
                _logger.error(f"PermissionLifecycleManager check failed: {e}")
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    async def _check_grants(self) -> None:
        """檢查所有授予狀態。"""
        now = datetime.now(timezone.utc)
        async with self._lock:
            for grant_id, grant in list(self._grants.items()):
                # 檢查過期
                if grant.is_expired() and grant.state == PermissionGrantState.ACTIVE:
                    if self.auto_renew_enabled and grant.auto_renew and grant.renewal_count < self._max_renewals:
                        await self._renew_grant(grant)
                    else:
                        grant.state = PermissionGrantState.EXPIRED
                        _logger.warning(f"Permission grant expired: {grant_id}")

                # 檢查即將過期
                elif grant.is_expiring_soon(self.expiry_warning_days) and grant.state == PermissionGrantState.ACTIVE:
                    grant.state = PermissionGrantState.EXPIRING
                    _logger.warning(f"Permission grant expiring soon: {grant_id}, expires at {grant.expires_at}")

                # 更新檢查時間
                grant.last_checked = now

                # 清理已終結（撤銷/過期）超過30天的授予 — EXPIRED 授予
                # 沒有 revoked_at，要用 expires_at 起算否則永不清除
                if grant.state in (PermissionGrantState.REVOKED, PermissionGrantState.EXPIRED):
                    terminal_at = grant.revoked_at or grant.expires_at
                    if terminal_at and (now - terminal_at) > timedelta(days=30):
                        del self._grants[grant_id]
                        _logger.info(f"Cleaned up old grant: {grant_id}")

    async def _renew_grant(self, grant: PermissionGrant) -> bool:
        """自動續期權限授予。

        續期是權限事務 — 委派給權限主宰的 ``permission.renew`` 裁決
        （A10/A11 完整閘門 + A313 星澄審查），不自作決定。裁決被拒
        或主宰不可用時 fail-closed 標記過期。
        """
        try:
            from core_system.codex_decision import SovereignRequest

            outcome = await self.permission_sovereign.handle(
                SovereignRequest(
                    intent="permission.renew",
                    subject="permission-grant",
                    requester="permission-automation",
                    payload={
                        "grant_id": grant.grant_id,
                        "actor": grant.actor,
                        "capability": grant.capability,
                        "action": grant.action,
                        "target": grant.target,
                        "data_scope": grant.data_scope,
                        "renewal_count": grant.renewal_count,
                    },
                )
            )
            if not getattr(outcome, "accepted", False):
                _logger.warning(
                    "Renewal adjudication refused for %s: %s",
                    grant.grant_id,
                    getattr(outcome, "refusal", None),
                )
                grant.state = PermissionGrantState.EXPIRED
                return False
            grant.expires_at = datetime.now(timezone.utc) + self._default_ttl
            grant.renewed_at = datetime.now(timezone.utc)
            grant.renewal_count += 1
            grant.state = PermissionGrantState.ACTIVE
            _logger.info(f"Auto-renewed permission grant: {grant.grant_id}, count: {grant.renewal_count}")
            return True
        except Exception as e:
            _logger.error(f"Failed to renew grant {grant.grant_id}: {e}")
            grant.state = PermissionGrantState.EXPIRED
            return False

    def get_stats(self) -> dict[str, Any]:
        """獲取統計信息。"""
        states = defaultdict(int)
        for grant in self._grants.values():
            states[grant.state.value] += 1
        return {
            "total_grants": len(self._grants),
            "by_state": dict(states),
            "auto_renew_enabled": self.auto_renew_enabled,
            "max_renewals": self._max_renewals,
        }


class DirectorySyncManager:
    """目錄同步管理器。

    負責：
    1. 跨主權目錄的同步
    2. 目錄完整性驗證
    3. 變更檢測與同步
    """

    def __init__(
        self,
        permission_sovereign: Any,
        sync_interval: float = 300.0,  # 5分鐘
    ) -> None:
        self.permission_sovereign = permission_sovereign
        self.sync_interval = sync_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_sync_hash: Optional[str] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="directory-sync-manager")
        _logger.info("DirectorySyncManager started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _logger.info("DirectorySyncManager stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._sync_directories()
            except Exception as e:
                _logger.error(f"Directory sync failed: {e}")
            try:
                await asyncio.sleep(self.sync_interval)
            except asyncio.CancelledError:
                break

    async def _sync_directories(self) -> None:
        """同步所有受管目錄。"""
        # 獲取當前目錄快照
        code_rules = code_rule_directory_snapshot()
        authority = directory_authority_snapshot()
        identities = identity_group_snapshot()
        permissions = identity_permission_snapshot()
        capabilities, repairs = capability_boundary_snapshot()
        policy = governance_policy_snapshot()

        # 計算同步雜湊
        import hashlib
        sync_data = f"{code_rules.initial_code_version}{authority.authority_version_policy.current_version}{len(identities.identities)}"
        current_hash = hashlib.sha256(sync_data.encode()).hexdigest()

        if self._last_sync_hash is None:
            self._last_sync_hash = current_hash
            return

        if current_hash != self._last_sync_hash:
            _logger.info("Directory changes detected, sync triggered")
            self._last_sync_hash = current_hash
            await self._notify_directory_children(sync_data)

    async def _notify_directory_children(self, sync_data: str) -> None:
        """Notify the permission-sovereign's codex children of the change.

        Directory and identity-group coordination belongs to
        ``directory-sub-sovereign`` / ``identity-group-sub-sovereign``
        (A316/A317) — delivery goes through the sovereign's
        ``delegate_to`` so the child gate sees the real parent as
        requester.  Undelivered notifications are logged, not raised.
        """
        from core_system.codex_decision import SovereignRequest

        for child_id in (
            "directory-sub-sovereign",
            "identity-group-sub-sovereign",
        ):
            try:
                outcome = await self.permission_sovereign.delegate_to(
                    child_id,
                    SovereignRequest(
                        intent="sync",
                        subject="directory-change",
                        requester="permission-automation",
                        payload={
                            "target": child_id,
                            "sync_status": "directory-changed",
                            "hash_source": sync_data[:64],
                        },
                    ),
                )
                if not getattr(outcome, "accepted", False):
                    _logger.warning(
                        "Directory sync notification to %s refused: %s",
                        child_id,
                        getattr(outcome, "refusal", None),
                    )
            except Exception as e:
                _logger.warning(
                    "Directory sync notification to %s failed: %s",
                    child_id,
                    e,
                )

    def get_sync_status(self) -> dict[str, Any]:
        return {
            "last_sync_hash": self._last_sync_hash,
            "sync_interval": self.sync_interval,
        }


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
        self._task: Optional[asyncio.Task] = None

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

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._check_compliance()
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

            # 檢查版本一致性
            if code_rules.initial_code_version != "1.0.0":
                _logger.warning("Code rule directory version mismatch")

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
        執行合規監督範圍（A6）。
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


class AuditScheduler:
    """審計排程器。

    負責：
    1. 定期自動執行治理審計
    2. 審計結果記錄與告警
    3. 審計歷史管理
    """

    def __init__(self, project_root: Path, interval: float = 3600.0) -> None:
        self.project_root = project_root
        self.interval = interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._audit_history: list[dict] = []

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="audit-scheduler")
        _logger.info("AuditScheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._run_audit()
            except Exception as e:
                _logger.error(f"Scheduled audit failed: {e}")
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break

    async def _run_audit(self) -> dict[str, Any]:
        """執行治理審計。"""
        import subprocess
        import sys

        try:
            result = subprocess.run(
                [sys.executable, "-m", "governance_rule.execution.audit"],
                cwd=str(Path(__file__).resolve().parents[3]),
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            output = (result.stdout or "") + (result.stderr or "")
            passed = "[PASS]" in output and result.returncode == 0

            audit_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "passed": passed,
                "output": output[:2000],
                "return_code": result.returncode,
            }
            self._audit_history.append(audit_record)

            if not passed:
                _logger.error(f"Scheduled governance audit FAILED: {output[:500]}")
            else:
                _logger.info("Scheduled governance audit PASSED")

            return audit_record

        except subprocess.TimeoutExpired:
            _logger.error("Scheduled audit timed out")
            return {"passed": False, "error": "timeout"}
        except Exception as e:
            _logger.error(f"Scheduled audit error: {e}")
            return {"passed": False, "error": str(e)}

    def get_audit_history(self, count: int = 50) -> list[dict]:
        return self._audit_history[-count:]


class SelfHealingManager:
    """自我修復管理器。

    負責：
    1. 檢測權限主宰組件健康狀態
    2. 自動修復常見問題
    3. 狀態恢復
    4. 降級模式管理
    """

    def __init__(self, permission_sovereign: Any, check_interval: float = 300.0) -> None:
        self.permission_sovereign = permission_sovereign
        self.check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._degraded_mode = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="self-healing-manager")
        _logger.info("SelfHealingManager started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._health_check()
            except Exception as e:
                _logger.error(f"Self-healing check failed: {e}")
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    async def _health_check(self) -> None:
        """健康檢查與自動修復。"""
        issues = []

        # 1. 檢查目錄可訪問性
        if not self._check_directory_access():
            issues.append("directory_access")

        # 2. 檢查治理連接
        if not self._check_governance_connection():
            issues.append("governance_connection")

        # 3. 檢查主權狀態
        if not self._check_sovereign_state():
            issues.append("sovereign_state")

        # 4. 檢查目錄權限
        if not self._check_directory_permissions():
            issues.append("directory_permissions")

        # 自動修復
        for issue in issues:
            await self._attempt_repair(issue)

        if issues:
            _logger.warning(f"Self-healing detected issues: {issues}")

    def _check_directory_access(self) -> bool:
        """檢查目錄可訪問性。"""
        try:
            code_rule_directory_snapshot()
            directory_authority_snapshot()
            identity_group_snapshot()
            return True
        except Exception:
            return False

    def _check_governance_connection(self) -> bool:
        """檢查治理連接 — 權限主宰必須能解析治理參考且已啟動。"""
        try:
            sovereign = self.permission_sovereign
            if not getattr(sovereign, "started", True):
                return False
            governance = getattr(sovereign, "_governance", None)
            if callable(governance):
                governance = governance()
            if governance is None:
                governance = getattr(getattr(sovereign, "app", None), "governance", None)
            return governance is not None
        except Exception:
            return False

    def _check_sovereign_state(self) -> bool:
        """檢查主權狀態。"""
        try:
            # 檢查權限主宰基本屬性
            return (
                hasattr(self.permission_sovereign, 'sovereign_id') and
                self.permission_sovereign.sovereign_id == "permission-sovereign"
            )
        except Exception:
            return False

    def _check_directory_permissions(self) -> bool:
        """檢查目錄權限。"""
        try:
            # 嘗試讀取受保護目錄
            from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
            dir_auth = directory_authority_snapshot()
            return dir_auth.authority_version_policy.current_version is not None
        except Exception:
            return False

    async def _attempt_repair(self, issue: str) -> None:
        """嘗試修復問題。"""
        _logger.warning(f"Attempting auto-repair for: {issue}")

        if issue == "directory_access":
            # 嘗試重新載入目錄
            try:
                code_rule_directory_snapshot()
                directory_authority_snapshot()
                identity_group_snapshot()
            except Exception as e:
                _logger.error(f"Failed to repair directory_access: {e}")

        elif issue == "governance_connection":
            # 觸發治理重新認證
            try:
                if hasattr(self.permission_sovereign, 're_certify'):
                    self.permission_sovereign.re_certify()
            except Exception as e:
                _logger.error(f"Failed to repair governance_connection: {e}")

        elif issue == "sovereign_state":
            # 重置主權狀態
            try:
                if hasattr(self.permission_sovereign, 're_certify'):
                    self.permission_sovereign.re_certify()
            except Exception as e:
                _logger.error(f"Failed to repair sovereign_state: {e}")

        elif issue == "directory_permissions":
            # 嘗試重新驗證目錄權限
            try:
                from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
                directory_authority_snapshot()
            except Exception as e:
                _logger.error(f"Failed to repair directory_permissions: {e}")

    def is_degraded(self) -> bool:
        """檢查是否處於降級模式。"""
        return self._degraded_mode


class IdentityGroupManager:
    """身份群組自動管理器。

    負責：
    1. 身份群組自動註冊/註銷
    2. 衝突檢測與解決
    3. 權限繼承管理
    4. 群組成員同步
    """

    def __init__(self, permission_sovereign: Any) -> None:
        self.permission_sovereign = permission_sovereign
        self._group_registry: dict[str, dict] = {}

    def register_group(
        self,
        group_id: str,
        actor: str,
        capabilities: list[str],
        tool_id: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """註冊新身份群組。"""
        if not group_id or not actor:
            return False
        # 檢查衝突
        if group_id in self._group_registry:
            return False

        self._group_registry[group_id] = {
            "actor": actor,
            "capabilities": capabilities,
            "tool_id": tool_id,
            "metadata": metadata or {},
            "registered_at": datetime.now(timezone.utc),
            "active": True,
            # A317: groups absent from the sealed identity registry are
            # coordinated-only — flagged as directory drift, never
            # silently authoritative.
            "directory_registered": self._in_directory(group_id),
        }
        _logger.info(f"Registered identity group: {group_id} for {actor}")
        return True

    @staticmethod
    def _in_directory(group_id: str) -> bool:
        try:
            registered = {
                getattr(identity, "group_id", None) or getattr(identity, "identity_code", None)
                for identity in identity_group_snapshot().identities
            }
            return group_id in registered
        except Exception:
            return False

    def reconcile_with_directory(self) -> dict[str, Any]:
        """對帳本地群組登錄與封印身份目錄。

        產生 drift 報告：目錄有但本地未登錄（missing）、本地有但目錄
        沒有（unregistered）。不回寫任何一邊 — 對帳是協調層證據，
        決策屬於權限主宰。
        """
        try:
            registered = {
                getattr(identity, "group_id", None) or getattr(identity, "identity_code", None)
                for identity in identity_group_snapshot().identities
            }
            registered.discard(None)
        except Exception as e:
            return {"ok": False, "error": f"directory-unavailable: {e}"}
        local = {gid for gid, info in self._group_registry.items() if info["active"]}
        return {
            "ok": True,
            "missing_locally": sorted(registered - local),
            "unregistered_in_directory": sorted(local - registered),
            "local_active": len(local),
            "directory_registered": len(registered),
        }

    def unregister_group(self, group_id: str) -> bool:
        """註銷身份群組。"""
        if group_id in self._group_registry:
            self._group_registry[group_id]["active"] = False
            self._group_registry[group_id]["unregistered_at"] = datetime.now(timezone.utc)
            return True
        return False

    def detect_conflicts(self) -> list[dict]:
        """檢測身份群組衝突。"""
        conflicts = []
        actors = defaultdict(list)

        for group_id, info in self._group_registry.items():
            if info["active"]:
                actors[info["actor"]].append(group_id)

        for actor, groups in actors.items():
            if len(groups) > 1:
                conflicts.append({
                    "actor": actor,
                    "groups": groups,
                    "type": "duplicate_actor",
                })

        return conflicts

    def resolve_conflicts(self) -> list[dict]:
        """解決衝突（保留最新註冊的）。"""
        resolved = []
        conflicts = self.detect_conflicts()

        for conflict in conflicts:
            groups = conflict["groups"]
            # 保留最新註冊的，停用其他的
            sorted_groups = sorted(
                groups,
                key=lambda g: self._group_registry[g]["registered_at"],
                reverse=True,
            )
            for group_id in sorted_groups[1:]:
                self.unregister_group(group_id)
                resolved.append({"group_id": group_id, "action": "deactivated"})

        return resolved

    def get_group_status(self, group_id: str) -> Optional[dict]:
        return self._group_registry.get(group_id)

    def list_active_groups(self) -> list[dict]:
        return [
            {"group_id": gid, **info}
            for gid, info in self._group_registry.items()
            if info["active"]
        ]


class PermissionAutomationOrchestrator:
    """權限自動化協調器。

    統一管理所有自動化組件：
    - 權限生命週期管理
    - 目錄同步
    - 合規監控
    - 審計排程
    - 自我修復
    - 身份群組管理
    """

    def __init__(
        self,
        permission_sovereign: Any,
        project_root: Optional[Path] = None,
    ) -> None:
        self.permission_sovereign = permission_sovereign
        self.project_root = project_root or Path(__file__).resolve().parents[3]

        # 初始化所有子組件
        self.lifecycle = PermissionLifecycleManager(permission_sovereign)
        self.directory_sync = DirectorySyncManager(permission_sovereign)
        self.compliance = ComplianceMonitor(permission_sovereign)
        self.audit = AuditScheduler(Path(__file__).resolve().parents[3])
        self.healing = SelfHealingManager(permission_sovereign)
        self.identity = IdentityGroupManager(permission_sovereign)

        self._running = False
        self._components: list[Any] = [
            self.lifecycle,
            self.directory_sync,
            self.compliance,
            self.audit,
            self.healing,
        ]

    async def start(self) -> None:
        """啟動所有自動化組件。"""
        if self._running:
            return

        for component in self._components:
            await component.start()

        self._running = True
        _logger.info("PermissionAutomationOrchestrator started")

    async def stop(self) -> None:
        """停止所有自動化組件。"""
        for component in reversed(self._components):
            await component.stop()

        self._running = False
        _logger.info("PermissionAutomationOrchestrator stopped")

    def get_system_status(self) -> dict[str, Any]:
        """獲取整體系統狀態。"""
        return {
            "lifecycle": self.lifecycle.get_stats(),
            "directory_sync": self.directory_sync.get_sync_status(),
            "compliance": self.compliance.get_risk_report(),
            "audit_history": self.audit.get_audit_history(10),
            "healing_degraded": self.healing.is_degraded(),
            "identity_groups": self.identity.list_active_groups(),
            "identity_directory_reconciliation": self.identity.reconcile_with_directory(),
        }

    # 代理方法 - 委派給權限主宰
    def register_grant(self, *args, **kwargs) -> Any:
        return self.lifecycle.register_grant(*args, **kwargs)

    def revoke_grant(self, *args, **kwargs) -> Any:
        return self.lifecycle.revoke_grant(*args, **kwargs)

    def register_identity_group(self, *args, **kwargs) -> Any:
        return self.identity.register_group(*args, **kwargs)


__all__ = [
    "PermissionGrantState",
    "ComplianceSeverity",
    "PermissionGrant",
    "ComplianceViolation",
    "AuditSchedule",
    "PermissionLifecycleManager",
    "DirectorySyncManager",
    "ComplianceMonitor",
    "AuditScheduler",
    "SelfHealingManager",
    "IdentityGroupManager",
    "PermissionAutomationOrchestrator",
]
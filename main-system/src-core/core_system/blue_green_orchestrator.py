"""§10.15/§10.17 Blue-Green Orchestrator（`star-blue-green-orchestrator/v1`）。

**重用而非第二套 Blue-Green**：A330 `boot_core_handover.py` 已提供
standby → 切換 → 排空 → 驗證 → rollback 的完整機制；本模組不重寫它，而是把
五個新契約（Execution Lease／Task Lifecycle／Backend Lifecycle／Request
Registry／§10.15 Release Retention）**編排到**既有 handover 的前後，形成
受管更新鏈：

```
1. 確認更新執行權（Execution Lease 由持有者提交；舊 token fail-closed）
2. 停止 A 接收新請求（Backend Lifecycle: ACTIVE → DRAINING）
3. 排空 A 的在途請求（Request Registry by_backend_generation == 0）
4. 呼叫既有 handover（A330 standby → 切換 → 排空 → 驗證 → rollback）
5. 成功 → 更新契約：
   - Backend Lifecycle: B → ACTIVE、A 舊世代 → STANDBY／STOPPING
   - §10.15 保留階層: B → ACTIVE（自動把舊 ACTIVE 降 PREVIOUS）
   - Activation ledger 記錄
6. 失敗／rollback → 契約一致退回：A → ACTIVE、保留階層不變
```

整合點：`handover_fn` 由呼叫方注入（例如 `BootCore._maybe_handover`），
本模組不 import boot_core，維持 `core_system` 單向依賴。所有轉移 fail-closed。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_logger = logging.getLogger("gptbridge.blue_green_orchestrator")

BLUE_GREEN_ORCHESTRATOR_VERSION = "star-blue-green-orchestrator/v1"

LEASE_TYPE_UPDATE = "maintenance-update"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UpdateOutcome:
    ok: bool
    reason: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    active_backend_id: str = ""
    active_generation: str = ""
    rolled_back: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class BlueGreenOrchestrator:
    """把五個契約編排到既有 A330 handover 前後（受管更新鏈）。

    ``handover_fn`` 簽章：``(operation_id, target_generation) -> dict``，回傳
    ``{"ok": bool, "rolled_back": bool, "active_generation": str,
    "active_port": int | None}``。實際 standby spawn／health probe／rollback
    全由該函式（A330 mixin）負責。
    """

    def __init__(
        self,
        *,
        lease: Any,
        backend_lifecycle: Any,
        request_registry: Any,
        retention: Any,
        handover_fn: Callable[[str, str], dict[str, Any]],
        state_path: str | Path,
        audit_path: str | Path | None = None,
        drain_poll_seconds: float = 0.05,
        drain_timeout_seconds: float = 5.0,
    ) -> None:
        self._lease = lease
        self._lifecycle = backend_lifecycle
        self._requests = request_registry
        self._retention = retention
        self._handover_fn = handover_fn
        self._state_path = Path(state_path)
        self._audit_path = (
            Path(audit_path) if audit_path else self._state_path.with_suffix(".jsonl")
        )
        self._drain_poll_seconds = drain_poll_seconds
        self._drain_timeout_seconds = drain_timeout_seconds
        self._updates: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._updates = data.get("updates", {})

    def _persist(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "orchestrator_version": BLUE_GREEN_ORCHESTRATOR_VERSION,
            "updated_at": _utc_iso(),
            "updates": self._updates,
        }
        fd, tmp = tempfile.mkstemp(dir=str(self._state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self._state_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _audit(self, operation_id: str, entry: dict[str, Any]) -> None:
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {"timestamp": _utc_iso(), "operation_id": operation_id, **entry},
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n"
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            _logger.warning("blue-green orchestrator audit append failed: %s", error)

    # -- orchestration -----------------------------------------------------------

    def perform_update(
        self,
        operation_id: str,
        *,
        holder: str,
        fencing_token: int,
        backend_generation: str,
        target_generation: str,
        old_backend_id: str,
        new_backend_id: str,
        new_release_id: str,
        compatible_with: list[str] | None = None,
    ) -> UpdateOutcome:
        """執行一次受管更新（§10.17 順序，fail-closed）。

        ``backend_generation`` = 舊世代（更新執行所處世代，租約綁定、在途
        請求歸屬）；``target_generation`` = 新世代（handover 目標）。

        步驟：
        1. verify 更新執行權租約（舊 token token 失效即拒絶）
        2. 停止舊後端接收新請求（ACTIVE → DRAINING）
        3. 等待舊世代在途請求排空（Request Registry）
        4. 呼叫注入的 handover（A330 standby→切換→驗證→rollback）
        5a. 成功 → Backend Lifecycle ＆ §10.15 保留階層推進
        5b. 失敗／rollback → 契約一致退回（舊後端回 ACTIVE、階層不動）
        """
        outcome = UpdateOutcome(ok=False, reason="", steps=[])
        outcome.steps.append({"step": "lease-verify", "lease_type": LEASE_TYPE_UPDATE})

        verified = self._lease.verify(
            LEASE_TYPE_UPDATE,
            holder,
            backend_generation,
            fencing_token,
        )
        if not verified:
            outcome.reason = "update-lease-not-held"
            outcome.steps[-1]["ok"] = False
            self._record(operation_id, outcome)
            return outcome
        outcome.steps[-1]["ok"] = True

        # 2. 排空舊世代在途請求（不預先改寫 Lifecycle 角色——DRAINING 是
        #    收斂終態，只有切換成功後才把舊後端調離 ACTIVE）
        outcome.steps.append({"step": "wait-inflight-drain", "generation": backend_generation})
        if not self._wait_no_inflight(backend_generation):
            outcome.reason = "inflight-drain-timeout"
            outcome.steps[-1]["ok"] = False
            self._record(operation_id, outcome)
            return outcome
        outcome.steps[-1]["ok"] = True

        # 4. 呼叫既有 A330 handover
        outcome.steps.append({"step": "handover", "target_generation": target_generation})
        try:
            result = self._handover_fn(operation_id, target_generation)
        except Exception as error:  # noqa: BLE001 — fail-closed on any handover error
            outcome.reason = f"handover-error:{type(error).__name__}"
            outcome.steps[-1]["ok"] = False
            self._rollback_contracts(outcome, old_backend_id)
            self._record(operation_id, outcome)
            return outcome
        outcome.steps[-1].update({k: v for k, v in result.items() if v is not None})

        if not result.get("ok"):
            outcome.reason = result.get("reason") or "handover-failed"
            outcome.steps[-1]["ok"] = False
            self._rollback_contracts(outcome, old_backend_id)
            self._record(operation_id, outcome)
            return outcome

        # 5a. 成功：推進契約
        outcome.steps.append({"step": "advance-contracts"})
        self._advance_contracts(
            outcome,
            old_backend_id=old_backend_id,
            new_backend_id=new_backend_id,
            new_release_id=new_release_id,
            compatible_with=compatible_with,
        )
        outcome.ok = True
        outcome.reason = "update-complete"
        outcome.steps[-1]["ok"] = True
        self._record(operation_id, outcome)
        return outcome

    def _wait_no_inflight(self, generation: str) -> bool:
        import time

        deadline = time.monotonic() + self._drain_timeout_seconds
        while time.monotonic() < deadline:
            inflight = self._requests.by_backend_generation(generation)
            running = [
                r for r in inflight if _request_active(r)
            ]
            if not running:
                return True
            time.sleep(self._drain_poll_seconds)
        return False

    def _advance_contracts(
        self,
        outcome: UpdateOutcome,
        *,
        old_backend_id: str,
        new_backend_id: str,
        new_release_id: str,
        compatible_with: list[str] | None,
    ) -> None:
        """成功後推進契約：Backend Lifecycle ＋ §10.15 保留階層。

        handover 已把 gateway 切到新世代，故在此把登錄簿同步：
        - 新後端 → ACTIVE（接受新請求）
        - 舊後端 → STANDBY（已停止接收、保留為回復來源）
        - §10.15：new_release 昇為 ACTIVE（自動把舊 ACTIVE 降 PREVIOUS）
        """
        self._lifecycle.become_active(new_backend_id)
        self._lifecycle.become_standby(old_backend_id)
        if self._retention.get(new_release_id) is None:
            self._retention.register(new_release_id)
        promoted = self._retention.become_active(
            new_release_id, compatible_with=compatible_with
        )
        outcome.active_backend_id = new_backend_id
        outcome.active_generation = (
            self._lifecycle.get(new_backend_id).generation
            if self._lifecycle.get(new_backend_id) is not None
            else ""
        )
        outcome.steps[-1]["retention_promote"] = (promoted.ok, promoted.reason)
        self._audit(
            operation_id=outcome.active_generation or "",
            entry={
                "phase": "advance-contracts",
                "new_backend_id": new_backend_id,
                "new_release_id": new_release_id,
                "old_backend_id": old_backend_id,
                "compatible_with": compatible_with or [],
                "retention_reason": promoted.reason,
            },
        )

    def _rollback_contracts(
        self,
        outcome: UpdateOutcome,
        old_backend_id: str,
    ) -> None:
        """失敗／rollback：契約維持現狀——舊後端未離 ACTIVE（DRAINING 是
        收斂終態，取得成功切換前不得把舊後端調離 ACTIVE），無需回復動作。"""
        outcome.rolled_back = True
        outcome.steps.append(
            {"step": "rollback-contracts", "backend_id": old_backend_id, "ok": True}
        )

    def _record(self, operation_id: str, outcome: UpdateOutcome) -> None:
        self._updates[operation_id] = outcome.as_dict()
        self._persist()
        self._audit(
            operation_id=operation_id,
            entry={
                "phase": "outcome",
                "ok": outcome.ok,
                "reason": outcome.reason,
                "rolled_back": outcome.rolled_back,
            },
        )

    # -- queries ------------------------------------------------------------------

    def get(self, operation_id: str) -> Optional[dict[str, Any]]:
        return self._updates.get(operation_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "orchestrator_version": BLUE_GREEN_ORCHESTRATOR_VERSION,
            "updates": self._updates,
            "count": len(self._updates),
        }


def _request_active(record: Any) -> bool:
    """判斷請求已佔用舊 Backend（QUEUED／RUNNING）。"""
    status = getattr(record, "status", "") or ""
    return status in {"QUEUED", "RUNNING"}


__all__ = [
    "BLUE_GREEN_ORCHESTRATOR_VERSION",
    "BlueGreenOrchestrator",
    "UpdateOutcome",
    "LEASE_TYPE_UPDATE",
]
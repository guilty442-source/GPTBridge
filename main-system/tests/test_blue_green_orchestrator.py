"""§10.15/§10.17 Blue-Green Orchestrator tests — contracts wired around A330 handover."""

from __future__ import annotations

import json

from core_system.blue_green_orchestrator import (
    LEASE_TYPE_UPDATE,
    BlueGreenOrchestrator,
)
from core_system.execution_lease import RuntimeExecutionLease
from core_system.backend_lifecycle import BackendLifecycleRegistry
from core_system.request_registry import RequestRegistry
from core_system.release_retention import ReleaseRetentionRegistry


def _make(tmp_path):
    lease = RuntimeExecutionLease(
        str(tmp_path / "lease.json"), ttl_seconds=60.0
    )
    lifecycle = BackendLifecycleRegistry(str(tmp_path / "lifecycle.json"))
    requests = RequestRegistry(str(tmp_path / "requests.json"))
    retention = ReleaseRetentionRegistry(
        str(tmp_path / "retention.json"),
        audit_path=str(tmp_path / "retention-audit.jsonl"),
    )
    handover_log: list[tuple[str, str]] = []

    def handover_fn(operation_id: str, target_generation: str) -> dict:
        handover_log.append((operation_id, target_generation))
        return {
            "ok": True,
            "rolled_back": False,
            "active_generation": target_generation,
            "active_port": 9082,
        }

    orchestrator = BlueGreenOrchestrator(
        lease=lease,
        backend_lifecycle=lifecycle,
        request_registry=requests,
        retention=retention,
        handover_fn=handover_fn,
        state_path=str(tmp_path / "orchestrator.json"),
        audit_path=str(tmp_path / "orchestrator-audit.jsonl"),
        drain_timeout_seconds=1.0,
    )
    return lease, lifecycle, requests, retention, orchestrator, handover_log


def _seed(lease, lifecycle, requests, retention):
    lease.acquire(LEASE_TYPE_UPDATE, "updater", "gen-1")
    token = lease.get(LEASE_TYPE_UPDATE).fencing_token
    lifecycle.register("backend-A", generation="gen-1", release_id="rel-1")
    lifecycle.become_active("backend-A")
    lifecycle.register("backend-B", generation="gen-2", release_id="rel-2")
    lifecycle.become_standby("backend-B")
    retention.register("rel-1")
    retention.become_active("rel-1")
    retention.register("rel-2")
    return token


def test_successful_update_advances_all_contracts(tmp_path):
    (
        lease,
        lifecycle,
        requests,
        retention,
        orchestrator,
        handover_log,
    ) = _make(tmp_path)
    token = _seed(lease, lifecycle, requests, retention)

    outcome = orchestrator.perform_update(
        "op-1",
        holder="updater",
        fencing_token=token,
        backend_generation="gen-1",
        target_generation="gen-2",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
        compatible_with=["rel-1"],
    )

    assert outcome.ok, outcome.reason
    assert outcome.active_backend_id == "backend-B"
    assert handover_log == [("op-1", "gen-2")]
    # Backend lifecycle: B → ACTIVE、A → STANDBY
    assert lifecycle.get("backend-B").lifecycle_role == "ACTIVE"
    assert lifecycle.get("backend-A").lifecycle_role == "STANDBY"
    # §10.15：rel-2 → ACTIVE（自動把 rel-1 降 PREVIOUS）
    assert retention.get("rel-2").tier == "ACTIVE"
    assert retention.get("rel-1").tier == "PREVIOUS"
    # 審計與狀態持久化
    rec = orchestrator.get("op-1")
    assert rec["ok"] is True
    snap = orchestrator.snapshot()
    assert snap["orchestrator_version"].startswith("star-blue-green-orchestrator")
    lines = [
        json.loads(line)
        for line in (tmp_path / "orchestrator-audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert any(line["phase"] == "advance-contracts" for line in lines)


def test_rejects_without_update_lease(tmp_path):
    (
        lease,
        lifecycle,
        requests,
        retention,
        orchestrator,
        handover_log,
    ) = _make(tmp_path)
    _seed(lease, lifecycle, requests, retention)

    outcome = orchestrator.perform_update(
        "op-x",
        holder="intruder",
        fencing_token=1,
        backend_generation="gen-1",
        target_generation="gen-2",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
    )

    assert outcome.ok is False
    assert outcome.reason == "update-lease-not-held"
    assert handover_log == []  # 未持有租約 → 不觸發 handover
    assert lifecycle.get("backend-A").lifecycle_role == "ACTIVE"


def test_drain_requires_no_inflight_requests(tmp_path):
    (
        lease,
        lifecycle,
        requests,
        retention,
        orchestrator,
        handover_log,
    ) = _make(tmp_path)
    orchestrator._drain_timeout_seconds = 0.05
    token = _seed(lease, lifecycle, requests, retention)
    # 舊世代（gen-1）已有一筆 RUNNING 在途請求 → 切換前必須等它排空
    requests.upsert("req-1", method="chat", status="RUNNING", backend_generation="gen-1")
    requests.mark_started("req-1")

    outcome = orchestrator.perform_update(
        "op-2",
        holder="updater",
        fencing_token=token,
        backend_generation="gen-1",
        target_generation="gen-2",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
    )

    # RUNNING 在途請求未排空 → 更新被拒絶（fail-closed），不觸發 handover；
    # 且舊後端維持 ACTIVE（DRAINING 是收斂終態，不得預先調離 ACTIVE）
    assert outcome.ok is False
    assert outcome.reason == "inflight-drain-timeout"
    assert handover_log == []
    assert lifecycle.get("backend-A").lifecycle_role == "ACTIVE"


def test_inflight_that_completes_allows_update(tmp_path):
    (
        lease,
        lifecycle,
        requests,
        retention,
        orchestrator,
        handover_log,
    ) = _make(tmp_path)
    token = _seed(lease, lifecycle, requests, retention)
    # 舊世代（gen-1）請求已完成 → 無在途請求，更新得以進行
    requests.upsert("req-1", method="chat", status="COMPLETED", backend_generation="gen-1")

    outcome = orchestrator.perform_update(
        "op-2b",
        holder="updater",
        fencing_token=token,
        backend_generation="gen-1",
        target_generation="gen-2",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
    )

    assert outcome.ok is True
    assert handover_log == [("op-2b", "gen-2")]


def test_inflight_request_on_old_generation_blocks_update(tmp_path):
    (
        lease,
        lifecycle,
        requests,
        retention,
        orchestrator,
        handover_log,
    ) = _make(tmp_path)
    orchestrator._drain_timeout_seconds = 0.05
    token = _seed(lease, lifecycle, requests, retention)
    requests.upsert("req-1", method="chat", status="RUNNING", backend_generation="gen-1")
    requests.mark_started("req-1")

    outcome = orchestrator.perform_update(
        "op-3",
        holder="updater",
        fencing_token=token,
        backend_generation="gen-1",
        target_generation="gen-2",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
    )

    # RUNNING 請求屬舊世代 → 排空階段會等待；timeout 後拒絶
    assert outcome.ok is False
    assert outcome.reason == "inflight-drain-timeout"
    assert handover_log == []


def test_rollback_on_handover_failure(tmp_path):
    (
        lease,
        lifecycle,
        requests,
        retention,
        orchestrator,
        _,
    ) = _make(tmp_path)
    lease.acquire(LEASE_TYPE_UPDATE, "updater", "gen-1")
    token = lease.get(LEASE_TYPE_UPDATE).fencing_token
    lifecycle.register("backend-A", generation="gen-1", release_id="rel-1")
    lifecycle.become_active("backend-A")
    lifecycle.register("backend-B", generation="gen-2", release_id="rel-2")
    lifecycle.become_standby("backend-B")
    retention.register("rel-1")
    retention.become_active("rel-1")

    def failing_handover(_op: str, _gen: str) -> dict:
        return {
            "ok": False,
            "rolled_back": True,
            "reason": "standby-unhealthy-after-activation",
            "active_generation": "gen-1",
            "active_port": 9081,
        }

    orchestrator._handover_fn = failing_handover
    outcome = orchestrator.perform_update(
        "op-4",
        holder="updater",
        fencing_token=token,
        backend_generation="gen-1",
        target_generation="gen-2",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
    )

    assert outcome.ok is False
    assert outcome.reason == "standby-unhealthy-after-activation"
    assert outcome.rolled_back is True
    # 契約一致退回：舊後端回 ACTIVE
    assert lifecycle.get("backend-A").lifecycle_role == "ACTIVE"
    # 保留階層不動：rel-1 仍 ACTIVE
    assert retention.get("rel-1").tier == "ACTIVE"


def test_persists_update_record(tmp_path):
    (
        lease,
        lifecycle,
        requests,
        retention,
        orchestrator,
        _,
    ) = _make(tmp_path)
    token = _seed(lease, lifecycle, requests, retention)
    orchestrator.perform_update(
        "op-5",
        holder="updater",
        fencing_token=token,
        backend_generation="gen-1",
        target_generation="gen-2",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
        compatible_with=["rel-1"],
    )

    reloaded = BlueGreenOrchestrator(
        lease=lease,
        backend_lifecycle=lifecycle,
        request_registry=requests,
        retention=retention,
        handover_fn=lambda _op, _gen: {},
        state_path=str(tmp_path / "orchestrator.json"),
    )
    rec = reloaded.get("op-5")
    assert rec is not None
    assert rec["ok"] is True
    state = json.loads((tmp_path / "orchestrator.json").read_text(encoding="utf-8"))
    assert state["orchestrator_version"].startswith("star-blue-green-orchestrator")